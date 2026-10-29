"""
Auth endpoints (sign-in, session refresh, sign-out, /auth/me, password
change) and the dependencies every other router uses for identity, role and
tenant checks.

Sessions: `POST /auth/login` returns a short-lived access token (JWT, kept in
the browser's memory, sent as `Authorization: Bearer`) and sets a refresh
token as an httpOnly, Secure, SameSite=Strict cookie scoped to the auth
routes. `POST /auth/refresh` rotates that cookie and returns a new access
token; `POST /auth/logout` ends the sign-in; `POST /auth/logout-all` ends every
sign-in of the account. The cookie routes also require the
`X-Requested-With: fddt-web` header — a cross-site page cannot send it
without a CORS preflight this API never allows (CSRF defence on top of
SameSite). See app/services/refresh_tokens.py.

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

The JWT carries `role`, `company_id` (null for platform admins),
`is_platform_admin` and `ver` (users.token_version), but the user is always
re-read from the database and the token must still match it — a moved,
demoted or deactivated account, a suspended company, a password change or a
sign-out everywhere takes effect on the next request.
"""
import uuid
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    access_token_lifetime_seconds,
    burn_password_check,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
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
from app.services import login_throttle, refresh_tokens
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
        "ver": user.token_version or 0,
    }


def _company_is_active(system_db: Session, company_id: uuid.UUID | None) -> bool:
    if company_id is None:
        return True
    active = system_db.execute(
        select(Company.is_active).where(Company.id == company_id)
    ).scalar_one_or_none()
    return bool(active)


# ---------------------------------------------------------------------------
# Session plumbing (access token + refresh cookie)
# ---------------------------------------------------------------------------

CSRF_HEADER = "X-Requested-With"
CSRF_HEADER_VALUE = "fddt-web"
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _require_csrf_header(request: Request) -> None:
    if request.headers.get(CSRF_HEADER) != CSRF_HEADER_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing request header.")


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.refresh_cookie_name,
        token,
        max_age=settings.refresh_token_idle_days * 86400,
        path=settings.refresh_cookie_path,
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite="strict",
    )


def _token_response(response: Response, user: User, refresh_token: str | None) -> TokenResponse:
    """Access token in the body; the refresh token (when there is a new one)
    only as the cookie."""
    response.headers.update(_NO_STORE)
    if refresh_token is not None:
        _set_refresh_cookie(response, refresh_token)
    return TokenResponse(
        access_token=create_access_token(subject=str(user.id), extra_claims=token_claims(user)),
        expires_in=access_token_lifetime_seconds(),
    )


def _session_401(detail: str) -> HTTPException:
    """401 that also clears the refresh cookie (FastAPI sends the exception's
    headers, not the injected Response's)."""
    probe = Response()
    _clear_refresh_cookie(probe)
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={**_UNAUTHORIZED, **_NO_STORE, "Set-Cookie": probe.headers["set-cookie"]},
    )


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
        raise _unauthorized("Invalid or expired token") from None

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
        if payload.get("ver") != (user.token_version or 0):
            raise _unauthorized("Your session has ended. Please sign in again.")
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
    summary="Sign in",
    description=(
        "Exchanges email + password for a short-lived access token (JWT, HS256, lifetime "
        "JWT_ACCESS_TOKEN_EXPIRE_MINUTES, default 15) in the body, and starts a session: a refresh "
        "token is set as an httpOnly, Secure, SameSite=Strict cookie (REFRESH_COOKIE_NAME, path "
        "REFRESH_COOKIE_PATH) used by `POST /auth/refresh`. The token carries `role`, `company_id` "
        "(null for a platform admin), `is_platform_admin` and `ver`. An unknown email, a wrong "
        "password, a deactivated account and a suspended company all return the same 401 (in the "
        "same time) so accounts cannot be enumerated. Throttled (app/services/login_throttle.py): "
        "after LOGIN_MAX_FAILED_ATTEMPTS failures an email address is refused for "
        "LOGIN_LOCKOUT_MINUTES, and each client IP gets LOGIN_MAX_ATTEMPTS_PER_IP_PER_MINUTE "
        "attempts per minute; both answer 429 with a Retry-After header."
    ),
    responses={
        401: {"description": "Incorrect email or password (or the account/company is inactive)."},
        429: {"description": "Too many sign-in attempts for this email address or from this IP."},
    },
)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    system_db: Session = Depends(get_system_db),
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
    if user is None:
        burn_password_check(payload.password)  # same timing as a wrong password
    if (
        user is None
        or not verify_password(payload.password, user.hashed_password)
        or not user.is_active
        or not _company_is_active(system_db, user.company_id)
    ):
        login_throttle.record_failure(email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    login_throttle.record_success(email)
    refresh_token, _row = refresh_tokens.issue(system_db, user, request)
    record_event(
        system_db,
        "user_signed_in",
        actor_user_id=user.id,
        company_id=user.company_id,
        event_data={
            "user_id": str(user.id),
            "email": user.email,
            "ip_address": request.client.host if request.client else None,
            "user_agent": (request.headers.get("user-agent") or "")[:255] or None,
        },
    )
    system_db.commit()
    return _token_response(response, user, refresh_token)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Renew the session",
    description=(
        "Exchanges the refresh-token cookie for a new access token and ROTATES the cookie (the "
        "old refresh token stops working). A refresh token that was already rotated and is "
        "presented again means it was copied: that whole sign-in is ended and the event is "
        "audited (`refresh_token_reuse_detected`). Requires the `X-Requested-With: fddt-web` "
        "header. No bearer token needed."
    ),
    responses={
        401: {"description": "No valid session: sign in again (the cookie is cleared)."},
        403: {"description": "The `X-Requested-With` header is missing."},
    },
)
def refresh_session(
    request: Request,
    response: Response,
    system_db: Session = Depends(get_system_db),
) -> TokenResponse:
    _require_csrf_header(request)
    token = request.cookies.get(settings.refresh_cookie_name, "")
    try:
        session = refresh_tokens.rotate(system_db, token, request)
    except refresh_tokens.RefreshError:
        raise _session_401("Your session has ended. Please sign in again.") from None
    system_db.commit()
    return _token_response(response, session.user, session.refresh_token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out",
    description=(
        "Ends this sign-in: its refresh tokens are revoked and the cookie is cleared. The current "
        "access token lapses on its own within minutes; the client must discard it. Requires the "
        "`X-Requested-With: fddt-web` header. Always 204."
    ),
    responses={403: {"description": "The `X-Requested-With` header is missing."}},
)
def logout(
    request: Request,
    response: Response,
    system_db: Session = Depends(get_system_db),
) -> None:
    _require_csrf_header(request)
    family = refresh_tokens.family_of(system_db, request.cookies.get(settings.refresh_cookie_name, ""))
    if family is not None:
        refresh_tokens.revoke_family(system_db, family, "logout")
        system_db.commit()
    _clear_refresh_cookie(response)
    response.headers.update(_NO_STORE)


@router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out everywhere",
    description=(
        "Ends every sign-in of the caller's account on every device: all refresh tokens are "
        "revoked and every access token already issued stops working immediately."
    ),
    responses={401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."}},
)
def logout_everywhere(
    response: Response,
    current_user: User = Depends(get_current_user),
    system_db: Session = Depends(get_system_db),
) -> None:
    state = tenancy.snapshot(system_db)
    tenancy.bind_platform(system_db)
    try:
        user = system_db.get(User, current_user.id)
        refresh_tokens.revoke_all_sessions(system_db, user, "revoked_all")
        record_event(
            system_db,
            "user_signed_out_everywhere",
            actor_user_id=user.id,
            company_id=user.company_id,
            event_data={"user_id": str(user.id), "email": user.email},
        )
        system_db.commit()
    finally:
        tenancy.restore(system_db, state)
    _clear_refresh_cookie(response)


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
        max_file_pages=limits.max_file_pages,
        max_file_size_bytes=limits.max_file_bytes,
        max_zip_size_bytes=limits.max_zip_bytes,
    )


@router.post(
    "/me/password",
    response_model=TokenResponse,
    summary="Change my password",
    description=(
        "Any signed-in user (every role) changes their own password by giving the current one. "
        "The new password (8-128 characters, different from the current one) is stored hashed and "
        "the change is recorded in the audit log (never the password). A forgotten password cannot "
        "be reset here: a platform admin resets it (`POST /settings/users/{id}/reset-password`). "
        "Every sign-in of the account, on every device, is ended; this one continues with the new "
        "access token and refresh cookie returned."
    ),
    responses={
        400: {"description": "The current password is wrong, or the new one equals it."},
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        422: {"description": "The new password is shorter than 8 or longer than 128 characters."},
    },
)
def change_my_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    system_db: Session = Depends(get_system_db),
) -> TokenResponse:
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
    if verify_password(payload.new_password, current_user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The new password must differ from the current one.")
    state = tenancy.snapshot(system_db)
    tenancy.bind_platform(system_db)
    try:
        user = system_db.get(User, current_user.id)
        user.hashed_password = hash_password(payload.new_password)
        refresh_tokens.revoke_all_sessions(system_db, user, "password_changed")
        refresh_token, _row = refresh_tokens.issue(system_db, user, request)
        record_event(
            system_db,
            "user_password_changed",
            actor_user_id=user.id,
            company_id=user.company_id,
            event_data={"user_id": str(user.id), "email": user.email},  # never the password
        )
        system_db.commit()
        return _token_response(response, user, refresh_token)
    finally:
        tenancy.restore(system_db, state)
