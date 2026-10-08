"""
Auth endpoints (POST /auth/login, GET /auth/me) and the dependencies every
other router uses for identity, role and tenant checks.

Every protected endpoint applies TWO mandatory checks:

1. Role — `require_company_role(min)` for company users (ranked user <
   reviewer_l1 < reviewer_l2), `require_platform_admin` for platform-only
   routes, or `require_reader(min)` for read-only routes that platform admins
   may also use for support.
2. Tenant — the request's data session (`get_tenant_db`) is bound to the
   caller's company, so every query it runs is filtered to that company in the
   application layer AND by PostgreSQL Row-Level Security. A resource id from
   another company therefore resolves to "not found" (404). Platform admins
   have no company: routes they may use bind the session to the company of the
   resource they are reading (app/api/tenant_access.py), and that access is
   audited.

The JWT carries `role`, `company_id` (null for platform admins) and
`is_platform_admin`, but the user is always re-read from the database and the
token's company must still match it — a moved, demoted or deactivated
account, or a suspended company, takes effect on the next request.
"""
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import create_access_token, decode_access_token, hash_password, verify_password
from app.db import tenancy
from app.db.session import get_db, get_system_db
from app.models.company import Company
from app.models.user import User, UserRole, has_rank, role_label
from app.schemas.auth import (
    ChangePasswordRequest,
    CurrentUserResponse,
    LoginRequest,
    TokenResponse,
    UploadLimitsResponse,
)
from app.services import login_throttle
from app.services.audit_service import record_event

router = APIRouter(prefix="/auth", tags=["auth"])

_bearer_scheme = HTTPBearer(auto_error=False)

_UNAUTHORIZED = {"WWW-Authenticate": "Bearer"}


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=detail, headers=_UNAUTHORIZED)


def token_claims(user: User) -> dict:
    return {
        "role": user.role.value,
        "company_id": str(user.company_id) if user.company_id else None,
        "is_platform_admin": user.is_platform_admin,
    }


def _company_is_active(system_db: Session, company_id: uuid.UUID | None) -> bool:
    if company_id is None:
        return True
    active = system_db.execute(
        select(Company.is_active).where(Company.id == company_id)
    ).scalar_one_or_none()
    return bool(active)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    system_db: Session = Depends(get_system_db),
) -> User:
    """The authenticated user, loaded through the platform path (identity
    lookup happens before we know which company the caller belongs to)."""
    if credentials is None:
        raise _unauthorized("Not authenticated")

    payload = decode_access_token(credentials.credentials)
    if payload is None or "sub" not in payload:
        raise _unauthorized("Invalid or expired token")

    try:
        user_id = uuid.UUID(payload["sub"])
    except (ValueError, TypeError):
        raise _unauthorized("Invalid or expired token")

    state = tenancy.snapshot(system_db)
    tenancy.bind_platform(system_db)
    try:
        user = system_db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
        if user is None or not user.is_active:
            raise _unauthorized("User not found or inactive")
        # The token's tenant claim must still describe this account: a user
        # moved to another company (or promoted out of one) needs a new token.
        claimed_company = payload.get("company_id")
        actual_company = str(user.company_id) if user.company_id else None
        if "company_id" in payload and claimed_company != actual_company:
            raise _unauthorized("Your account's company changed. Please sign in again.")
        if bool(payload.get("is_platform_admin", user.is_platform_admin)) != user.is_platform_admin:
            raise _unauthorized("Your account's role changed. Please sign in again.")
        if not _company_is_active(system_db, user.company_id):
            raise _unauthorized("Your company's account is suspended.")
    finally:
        tenancy.restore(system_db, state)
    return user


@dataclass(frozen=True)
class AuthContext:
    """Who is calling and which company they are confined to."""

    user: User

    @property
    def is_platform_admin(self) -> bool:
        return self.user.is_platform_admin

    @property
    def company_id(self) -> uuid.UUID | None:
        return self.user.company_id


def get_auth_context(user: User = Depends(get_current_user)) -> AuthContext:
    return AuthContext(user=user)


def get_tenant_db(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> Session:
    """The request's data session, bound to the caller's company. For a
    platform admin it is returned unbound — a platform route must bind it to
    the company it is acting on (app/api/tenant_access.py) before use, and an
    unbound session refuses to touch tenant data."""
    if not ctx.is_platform_admin:
        tenancy.bind_company(db, ctx.company_id)
    return db


_NO_PERMISSION = "You don't have permission to perform this action."


def require_company_role(minimum: UserRole):
    """403 unless the caller is a COMPANY user ranked at least `minimum`.
    Platform admins are refused: they have read-only support access and
    never act inside a company (approve, upload, ...)."""

    def dependency(current_user: User = Depends(get_current_user)) -> User:
        if current_user.is_platform_admin:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Platform admins have read-only support access to company data.",
            )
        if not has_rank(current_user.role, minimum):
            raise HTTPException(status.HTTP_403_FORBIDDEN, _NO_PERMISSION)
        return current_user

    return dependency


def require_reader(minimum: UserRole):
    """Read-only routes: company users ranked at least `minimum`, plus
    platform admins (support access — audited per resource by
    app/api/tenant_access.py)."""

    def dependency(current_user: User = Depends(get_current_user)) -> User:
        if current_user.is_platform_admin or has_rank(current_user.role, minimum):
            return current_user
        raise HTTPException(status.HTTP_403_FORBIDDEN, _NO_PERMISSION)

    return dependency


def require_platform_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_platform_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requires the platform admin role.")
    return current_user


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in and obtain a JWT",
    description=(
        "Exchanges email + password for a bearer token (HS256; lifetime from "
        "JWT_ACCESS_TOKEN_EXPIRE_MINUTES). The token carries `role`, `company_id` (null for a "
        "platform admin) and `is_platform_admin`. An unknown email, a wrong password, a "
        "deactivated account and a suspended company all return the same 401 so accounts "
        "cannot be enumerated. Throttled (app/services/login_throttle.py): after "
        "LOGIN_MAX_FAILED_ATTEMPTS failures an email address is refused for LOGIN_LOCKOUT_MINUTES, "
        "and each client IP gets LOGIN_MAX_ATTEMPTS_PER_IP_PER_MINUTE attempts per minute; both "
        "answer 429 with a Retry-After header."
    ),
    responses={
        401: {"description": "Incorrect email or password (or the account/company is inactive)."},
        429: {"description": "Too many sign-in attempts for this email address or from this IP."},
    },
)
def login(
    payload: LoginRequest, request: Request, system_db: Session = Depends(get_system_db)
) -> TokenResponse:
    email = str(payload.email)
    blocked = login_throttle.check(email, request.client.host if request.client else None)
    if blocked is not None:
        minutes = max(1, -(-blocked.retry_after_seconds // 60))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many sign-in attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.",
            headers={"Retry-After": str(blocked.retry_after_seconds)},
        )
    user = system_db.execute(
        select(User).where(func.lower(User.email) == str(payload.email).lower())
    ).scalar_one_or_none()
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.hashed_password)
        or not _company_is_active(system_db, user.company_id)
    ):
        login_throttle.record_failure(email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    login_throttle.record_success(email)
    access_token = create_access_token(subject=str(user.id), extra_claims=token_claims(user))
    return TokenResponse(access_token=access_token)


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="Get the current user",
    description=(
        "Returns the authenticated user's profile, role and company. The role and company are "
        "re-read from the database on every request rather than trusted from the token, so a "
        "demotion, deactivation or company suspension takes effect immediately."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
    },
)
def read_current_user(
    current_user: User = Depends(get_current_user),
    system_db: Session = Depends(get_system_db),
) -> CurrentUserResponse:
    company_name = None
    if current_user.company_id is not None:
        company_name = system_db.execute(
            select(Company.name).where(Company.id == current_user.company_id)
        ).scalar_one_or_none()
    return CurrentUserResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=current_user.role,
        role_label=role_label(current_user.role),
        is_active=current_user.is_active,
        is_platform_admin=current_user.is_platform_admin,
        company_id=current_user.company_id,
        company_name=company_name,
    )


@router.get(
    "/me/upload-limits",
    response_model=UploadLimitsResponse,
    summary="Get my company's upload limits",
    description=(
        "The per-file and per-zip limits the caller's company is held to (set per company by a "
        "platform admin). Read fresh on every request, so upload screens always show the limit that "
        "will actually be enforced. Platform admins belong to no company: 404."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        404: {"description": "Platform admins have no company and therefore no upload limits."},
    },
)
def read_upload_limits(
    current_user: User = Depends(get_current_user),
    system_db: Session = Depends(get_system_db),
) -> UploadLimitsResponse:
    from app.services.upload_limits import company_upload_limits

    if current_user.company_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Platform admins have no upload limits.")
    state = tenancy.snapshot(system_db)
    tenancy.bind_platform(system_db)
    try:
        limits = company_upload_limits(system_db, current_user.company_id)
    finally:
        tenancy.restore(system_db, state)
    return UploadLimitsResponse(
        max_file_size_mb=limits.max_file_size_mb,
        max_zip_size_mb=limits.max_zip_size_mb,
        max_file_size_bytes=limits.max_file_bytes,
        max_zip_size_bytes=limits.max_zip_bytes,
    )


@router.post(
    "/me/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change my password",
    description=(
        "Any signed-in user (every role) changes their own password by giving the current one. "
        "The new password (8-128 characters, different from the current one) is stored hashed and "
        "the change is recorded in the audit log (never the password). A forgotten password cannot "
        "be reset here: a platform admin resets it (`POST /settings/users/{id}/reset-password`). "
        "Tokens already issued stay valid until they expire."
    ),
    responses={
        400: {"description": "The current password is wrong, or the new one equals it."},
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        422: {"description": "The new password is shorter than 8 or longer than 128 characters."},
    },
)
def change_my_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    system_db: Session = Depends(get_system_db),
) -> None:
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
    if verify_password(payload.new_password, current_user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The new password must differ from the current one.")
    state = tenancy.snapshot(system_db)
    tenancy.bind_platform(system_db)
    try:
        user = system_db.get(User, current_user.id)
        user.hashed_password = hash_password(payload.new_password)
        record_event(
            system_db,
            "user_password_changed",
            actor_user_id=user.id,
            company_id=user.company_id,
            event_data={"user_id": str(user.id), "email": user.email},  # never the password
        )
        system_db.commit()
    finally:
        tenancy.restore(system_db, state)
