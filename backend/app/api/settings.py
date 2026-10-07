"""
Settings API: issuer registry, risk rules + thresholds (per company), users
(platform-wide).

Issuer registry and risk rules/thresholds are PER COMPANY — each company has
its own independent rows. They are managed by that company's `reviewer_l2`
(always confined to their own company) and by platform admins, who must pass
`company_id` and whose every read/change is audited in that company's log
(app/api/tenant_access.py).

Users are platform-admin-only: platform admins create every account and
assign it to a company + role (companies cannot create or manage users).

Every change writes an `audit_log` row (the same table and `record_event`
writer the rest of the app uses; nothing parallel), which the Audit History
screen and each rule's change trail read from.
"""
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import get_current_user, require_platform_admin
from app.api.tenant_access import CompanyScope, company_scope
from app.core.security import hash_password
from app.db.session import get_system_db
from app.models.company import Company
from app.models.issuer_registry import IssuerRegistry
from app.models.risk_rule import RiskRule
from app.models.user import COMPANY_ROLES, User, UserRole, has_rank
from app.schemas.settings import (
    IssuerCreate,
    IssuerResponse,
    IssuerUpdate,
    RiskRuleCreate,
    RiskRuleResponse,
    RiskRuleUpdate,
    RiskThresholds,
    RiskThresholdsResponse,
    UserAdminResponse,
    UserAdminUpdate,
    UserCreate,
    UserPasswordReset,
)
from app.services.audit_service import record_event
from app.services import risk_rule_catalog as catalog
from app.services.risk_scoring_service import get_risk_settings, load_current_rules

router = APIRouter(prefix="/settings", tags=["settings"])


def require_company_settings_access(current_user: User = Depends(get_current_user)) -> User:
    """Issuer registry / risk rules: a company's reviewer_l2 (own company
    only) or a platform admin (any company, audited)."""
    if current_user.is_platform_admin or has_rank(current_user.role, UserRole.reviewer_l2):
        return current_user
    raise HTTPException(
        status.HTTP_403_FORBIDDEN, "Requires the Reviewer L2 role (or platform admin)."
    )


_settings_user = require_company_settings_access
_issuer_scope = company_scope("Settings > Issuer Registry")
_rules_scope = company_scope("Settings > Risk Rules")


def _actor(db: Session, user_id: uuid.UUID | None) -> str | None:
    """Display name of who made a change. A change made by a platform admin
    is attributed to "Platform admin" in a company session (platform
    accounts are outside every company)."""
    if user_id is None:
        return None
    user = db.get(User, user_id)
    return (user.full_name or user.email) if user else "Platform admin"


# ---------------------------------------------------------------------------
# Issuer registry
# ---------------------------------------------------------------------------

@router.get(
    "/issuers",
    response_model=list[IssuerResponse],
    summary="List issuers",
    description=(
        "The company's issuer registry, ordered by name. Pass `include_inactive=false` to hide "
        "deactivated entries. Reviewer L2 (own company) or platform admin (`company_id`)."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires the Reviewer L2 role (or platform admin)."},
    },
)
def list_issuers(
    include_inactive: bool = Query(default=True),
    _user: User = Depends(_settings_user),
    scope: CompanyScope = Depends(_issuer_scope),
) -> list[IssuerRegistry]:
    db = scope.db
    stmt = (
        select(IssuerRegistry)
        .where(IssuerRegistry.company_id == scope.company_id)
        .order_by(IssuerRegistry.name)
    )
    if not include_inactive:
        stmt = stmt.where(IssuerRegistry.is_active.is_(True))
    return list(db.execute(stmt).scalars().all())


@router.post(
    "/issuers",
    response_model=IssuerResponse,
    status_code=201,
    summary="Create an issuer",
    description=(
        "Adds a known vendor/school/etc. to the company's registry used by issuer verification. "
        "Names are unique (case-insensitively) among the company's active issuers."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires the Reviewer L2 role (or platform admin)."},
        409: {"description": "An active issuer with this name already exists."},
    },
)
def create_issuer(
    payload: IssuerCreate,
    admin: User = Depends(_settings_user),
    scope: CompanyScope = Depends(_issuer_scope),
) -> IssuerRegistry:
    db = scope.db
    duplicate = db.execute(
        select(IssuerRegistry.id).where(
            IssuerRegistry.company_id == scope.company_id,
            func.lower(IssuerRegistry.name) == payload.name.lower(),
            IssuerRegistry.is_active.is_(True),
        )
    ).first()
    if duplicate is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "An active issuer with this name already exists.")

    issuer = IssuerRegistry(
        company_id=scope.company_id,
        name=payload.name,
        name_arabic=payload.name_arabic,
        tax_id=payload.tax_id,
        type=payload.type,
        is_active=True,
    )
    db.add(issuer)
    db.flush()
    record_event(
        db,
        "issuer_created",
        actor_user_id=admin.id,
        event_data={"issuer_id": str(issuer.id), "name": issuer.name, "type": issuer.type.value},
    )
    db.commit()
    db.refresh(issuer)
    return issuer


@router.patch(
    "/issuers/{issuer_id}",
    response_model=IssuerResponse,
    summary="Update or deactivate an issuer",
    description=(
        "Partial update: only the fields sent are changed. Issuers are never deleted; set "
        "`is_active=false` to retire one. A request that changes nothing is a no-op. An issuer "
        "of another company is a 404."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires the Reviewer L2 role (or platform admin)."},
        404: {"description": "No such issuer."},
    },
)
def update_issuer(
    issuer_id: uuid.UUID,
    payload: IssuerUpdate,
    admin: User = Depends(_settings_user),
    scope: CompanyScope = Depends(_issuer_scope),
) -> IssuerRegistry:
    db = scope.db
    issuer = db.execute(
        select(IssuerRegistry).where(
            IssuerRegistry.id == issuer_id, IssuerRegistry.company_id == scope.company_id
        )
    ).scalar_one_or_none()
    if issuer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Issuer not found")

    changes: dict[str, dict[str, Any]] = {}
    for name in payload.model_fields_set:
        new = getattr(payload, name)
        if name in ("name", "type", "is_active") and new is None:
            continue  # required columns: an explicit null just means "unchanged"
        old = getattr(issuer, name)
        old_cmp = old.value if hasattr(old, "value") else old
        new_cmp = new.value if hasattr(new, "value") else new
        if old_cmp != new_cmp:
            changes[name] = {"from": old_cmp, "to": new_cmp}
            setattr(issuer, name, new)
    if not changes:
        return issuer

    if "is_active" in changes:
        event = "issuer_reactivated" if changes["is_active"]["to"] else "issuer_deactivated"
    else:
        event = "issuer_updated"
    record_event(
        db,
        event,
        actor_user_id=admin.id,
        event_data={"issuer_id": str(issuer.id), "name": issuer.name, "changes": changes},
    )
    db.commit()
    db.refresh(issuer)
    return issuer


# ---------------------------------------------------------------------------
# Risk rules (versioned — an edit INSERTs version + 1, never updates a row)
# ---------------------------------------------------------------------------

def _rule_response(db: Session, rule: RiskRule, names: dict[uuid.UUID, str | None]) -> RiskRuleResponse:
    if rule.updated_by is not None and rule.updated_by not in names:
        names[rule.updated_by] = _actor(db, rule.updated_by)
    return RiskRuleResponse(
        id=rule.id,
        rule_id=rule.rule_id,
        category=rule.category,
        check_type=rule.check_type,
        weight=rule.weight,
        severity=rule.severity,
        reason_template=rule.reason_template,
        is_active=rule.is_active,
        version=rule.version,
        effective_from=rule.effective_from,
        updated_by_name=names.get(rule.updated_by) if rule.updated_by else None,
        change_note=rule.change_note,
    )


@router.get(
    "/risk-rules",
    response_model=list[RiskRuleResponse],
    summary="List current risk rules",
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires the Reviewer L2 role (or platform admin)."},
    },
)
def list_risk_rules(
    _user: User = Depends(_settings_user),
    scope: CompanyScope = Depends(_rules_scope),
) -> list[RiskRuleResponse]:
    """The CURRENT version of every rule (active or not) of the company, by
    category."""
    names: dict[uuid.UUID, str | None] = {}
    return [
        _rule_response(scope.db, r, names)
        for r in load_current_rules(scope.db, scope.company_id, include_inactive=True)
    ]


@router.get(
    "/risk-rule-options",
    summary="Get the options for the Add-rule form",
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires the Reviewer L2 role (or platform admin)."},
    },
)
def risk_rule_options(_user: User = Depends(_settings_user)) -> dict[str, Any]:
    """Everything the Add-rule form offers, so it never has to hardcode (or
    guess) what the checks can produce."""
    def label(value: str) -> str:
        return value.replace("_", " ").capitalize()

    return {
        "categories": [{"value": c, "label": label(c)} for c in catalog.CATEGORIES],
        "match_kinds": [{"value": v, "label": lbl, "help": h} for v, lbl, h in catalog.MATCH_KINDS],
        "finding_checks": [
            {"value": ct, "label": label(ct), "findings": names} for ct, names in catalog.FINDING_NAMES.items()
        ],
        "check_result_checks": [{"value": ct, "label": label(ct)} for ct in catalog.CHECK_RESULT_TYPES],
        "sub_checks": [{"value": s, "label": label(s)} for s in catalog.SUB_CHECKS],
        "cross_fields": [{"value": f, "label": label(f)} for f in catalog.CROSS_FIELDS],
        "signature_results": [{"value": r, "label": label(r)} for r in catalog.SIGNATURE_RESULTS],
        "placeholders": catalog.PLACEHOLDERS,
    }


@router.post(
    "/risk-rules",
    response_model=RiskRuleResponse,
    status_code=201,
    summary="Create a risk rule",
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires the Reviewer L2 role (or platform admin)."},
        409: {"description": "A rule with this `rule_id` already exists in the company."},
        422: {"description": "The chosen match/parameters cannot be evaluated by the engine."},
    },
)
def create_risk_rule(
    payload: RiskRuleCreate,
    admin: User = Depends(_settings_user),
    scope: CompanyScope = Depends(_rules_scope),
) -> RiskRuleResponse:
    """A new rule at version 1, for this company only. It applies to the
    company's cases scored from now on; nothing already scored changes
    (assessments freeze the versions that produced them)."""
    db = scope.db
    if db.execute(
        select(RiskRule.id)
        .where(RiskRule.company_id == scope.company_id, RiskRule.rule_id == payload.rule_id)
        .limit(1)
    ).first():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A rule called '{payload.rule_id}' already exists — edit it from the list, or choose another name.",
        )
    check_type, condition = catalog.build_condition(
        payload.match, check_type=payload.check_type, finding=payload.finding,
        severity_in=payload.severity_in, sub_check=payload.sub_check, field_name=payload.field_name,
        signature_result=payload.signature_result,
    )
    rule = RiskRule(
        company_id=scope.company_id,
        rule_id=payload.rule_id,
        category=payload.category,
        check_type=check_type,
        condition=condition,
        weight=payload.weight,
        severity=payload.severity,
        reason_template=payload.reason_template,
        is_active=payload.is_active,
        version=1,
        effective_from=datetime.now(timezone.utc),
        updated_by=admin.id,
        change_note=(payload.change_note or "").strip() or "Rule created",
    )
    db.add(rule)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A rule with that name was just created — reload and retry.")
    record_event(
        db,
        "risk_rule_created",
        actor_user_id=admin.id,
        event_data={
            "rule_id": rule.rule_id, "category": rule.category, "check_type": rule.check_type,
            "condition": condition, "weight": rule.weight, "severity": rule.severity, "is_active": rule.is_active,
        },
    )
    db.commit()
    db.refresh(rule)
    return _rule_response(db, rule, {})


@router.get(
    "/risk-rules/{rule_id}/history",
    response_model=list[RiskRuleResponse],
    summary="Get a risk rule's version history",
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires the Reviewer L2 role (or platform admin)."},
        404: {"description": "No such rule."},
    },
)
def risk_rule_history(
    rule_id: str,
    _user: User = Depends(_settings_user),
    scope: CompanyScope = Depends(_rules_scope),
) -> list[RiskRuleResponse]:
    """Every version of one of the company's rules, newest first — the
    per-rule audit trail (who changed what, when). The same edits are also in
    `audit_log`."""
    db = scope.db
    rows = db.execute(
        select(RiskRule)
        .where(RiskRule.company_id == scope.company_id, RiskRule.rule_id == rule_id)
        .order_by(RiskRule.version.desc())
    ).scalars().all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Risk rule not found")
    names: dict[uuid.UUID, str | None] = {}
    return [_rule_response(db, r, names) for r in rows]


@router.patch(
    "/risk-rules/{rule_id}",
    response_model=RiskRuleResponse,
    summary="Change a risk rule's weight, severity or active flag",
    description=(
        "Rules are versioned: an edit inserts a new immutable row (`version` + 1) and leaves "
        "the old one untouched, so every historical assessment stays truthful. Only `weight`, "
        "`severity` and `is_active` can be changed; at least one must differ. Only the "
        "company's own rule is affected."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires the Reviewer L2 role (or platform admin)."},
        400: {"description": "Nothing to change."},
        404: {"description": "No such rule."},
        409: {"description": "The rule was changed concurrently; reload and retry."},
    },
)
def update_risk_rule(
    rule_id: str,
    payload: RiskRuleUpdate,
    admin: User = Depends(_settings_user),
    scope: CompanyScope = Depends(_rules_scope),
) -> RiskRuleResponse:
    db = scope.db
    current = db.execute(
        select(RiskRule)
        .where(RiskRule.company_id == scope.company_id, RiskRule.rule_id == rule_id)
        .order_by(RiskRule.version.desc())
    ).scalars().first()
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Risk rule not found")

    new_weight = current.weight if payload.weight is None else payload.weight
    new_severity = current.severity if payload.severity is None else payload.severity
    new_active = current.is_active if payload.is_active is None else payload.is_active

    changes: dict[str, dict[str, Any]] = {}
    if new_weight != current.weight:
        changes["weight"] = {"from": current.weight, "to": new_weight}
    if new_severity != current.severity:
        changes["severity"] = {"from": current.severity, "to": new_severity}
    if new_active != current.is_active:
        changes["is_active"] = {"from": current.is_active, "to": new_active}
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No changes to save.")

    # New immutable row; the old version is left exactly as it was so every
    # historical assessment that references it stays truthful.
    new_row = RiskRule(
        company_id=scope.company_id,
        rule_id=current.rule_id,
        category=current.category,
        check_type=current.check_type,
        condition=current.condition,
        weight=new_weight,
        severity=new_severity,
        reason_template=current.reason_template,
        is_active=new_active,
        version=current.version + 1,
        effective_from=datetime.now(timezone.utc),
        updated_by=admin.id,
        change_note=(payload.change_note or "").strip() or None,
    )
    db.add(new_row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This rule was just changed by someone else — reload and retry."
        )
    record_event(
        db,
        "risk_rule_updated",
        actor_user_id=admin.id,
        event_data={
            "rule_id": rule_id,
            "from_version": current.version,
            "to_version": new_row.version,
            "changes": changes,
            "note": new_row.change_note,
        },
    )
    db.commit()
    db.refresh(new_row)
    return _rule_response(db, new_row, {})


@router.get(
    "/risk-thresholds",
    response_model=RiskThresholdsResponse,
    summary="Get the risk tier thresholds",
    description=(
        "The company's scores at which a case becomes `medium` and `high` risk (defaults 30 and "
        "60). The row is created on first read if it does not exist."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires the Reviewer L2 role (or platform admin)."},
    },
)
def get_risk_thresholds(
    _user: User = Depends(_settings_user),
    scope: CompanyScope = Depends(_rules_scope),
) -> RiskThresholdsResponse:
    db = scope.db
    row = get_risk_settings(db, scope.company_id)
    db.commit()  # persists the default row if it had to be created
    return RiskThresholdsResponse(
        medium_threshold=row.medium_threshold,
        high_threshold=row.high_threshold,
        metadata_score_cap=row.metadata_score_cap,
        updated_at=row.updated_at,
        updated_by_name=_actor(db, row.updated_by),
    )


@router.put(
    "/risk-thresholds",
    response_model=RiskThresholdsResponse,
    summary="Set the risk tier thresholds",
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires the Reviewer L2 role (or platform admin)."},
    },
)
def update_risk_thresholds(
    payload: RiskThresholds,
    admin: User = Depends(_settings_user),
    scope: CompanyScope = Depends(_rules_scope),
) -> RiskThresholdsResponse:
    """Safe for history: each assessment embeds the thresholds it used."""
    db = scope.db
    row = get_risk_settings(db, scope.company_id)
    old = {"medium": row.medium_threshold, "high": row.high_threshold, "metadata_cap": row.metadata_score_cap}
    new = {
        "medium": payload.medium_threshold,
        "high": payload.high_threshold,
        "metadata_cap": payload.metadata_score_cap or row.metadata_score_cap,
    }
    if old != new:
        row.medium_threshold = payload.medium_threshold
        row.high_threshold = payload.high_threshold
        row.metadata_score_cap = new["metadata_cap"]
        row.updated_by = admin.id
        record_event(
            db,
            "risk_thresholds_updated",
            actor_user_id=admin.id,
            event_data={"from": old, "to": new},
        )
    db.commit()
    db.refresh(row)
    return RiskThresholdsResponse(
        medium_threshold=row.medium_threshold,
        high_threshold=row.high_threshold,
        metadata_score_cap=row.metadata_score_cap,
        updated_at=row.updated_at,
        updated_by_name=_actor(db, row.updated_by),
    )


# ---------------------------------------------------------------------------
# Users — platform admin only (companies cannot create or manage users)
# ---------------------------------------------------------------------------

_PLATFORM_ONLY = {403: {"description": "Requires the platform admin role."}}


def _company_names(db: Session) -> dict[uuid.UUID, str]:
    return {c.id: c.name for c in db.execute(select(Company)).scalars().all()}


def _user_response(user: User, names: dict[uuid.UUID, str]) -> UserAdminResponse:
    return UserAdminResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
        company_id=user.company_id,
        company_name=names.get(user.company_id) if user.company_id else None,
    )


def _require_active_company(db: Session, company_id: uuid.UUID | None) -> Company:
    if company_id is None:
        raise HTTPException(422, "Company users must be assigned to a company.")
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    return company


@router.get(
    "/users",
    response_model=list[UserAdminResponse],
    summary="List users",
    description=(
        "Every account on the platform, ordered by company then email; filter with "
        "`company_id`. Platform admin only."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        **_PLATFORM_ONLY,
    },
)
def list_users(
    company_id: uuid.UUID | None = Query(default=None),
    _admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_system_db),
) -> list[UserAdminResponse]:
    stmt = select(User).order_by(User.company_id.is_(None).desc(), User.company_id, User.email)
    if company_id is not None:
        stmt = stmt.where(User.company_id == company_id)
    names = _company_names(db)
    return [_user_response(u, names) for u in db.execute(stmt).scalars().all()]


@router.post(
    "/users",
    response_model=UserAdminResponse,
    status_code=201,
    summary="Create a user",
    description=(
        "Creates an account and assigns it to a company + role. Company roles (`user`, "
        "`reviewer_l1`, `reviewer_l2`) need a `company_id`; a `platform_admin` must have none. "
        "Platform admin only."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        **_PLATFORM_ONLY,
        404: {"description": "No such company."},
        409: {"description": "An account with that email already exists."},
        422: {"description": "Company/role combination is invalid."},
    },
)
def create_user(
    payload: UserCreate,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_system_db),
) -> UserAdminResponse:
    """Create an account. Emails are unique (platform-wide) regardless of
    letter case."""
    email = str(payload.email).strip()
    if payload.role == UserRole.platform_admin:
        company = None
    else:
        company = _require_active_company(db, payload.company_id)
    if db.execute(select(User.id).where(func.lower(User.email) == email.lower()).limit(1)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with that email already exists.")
    user = User(
        email=email,
        full_name=payload.full_name,
        role=payload.role,
        company_id=company.id if company else None,
        hashed_password=hash_password(payload.password),
        is_active=True,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with that email already exists.")
    record_event(
        db,
        "user_created",
        actor_user_id=admin.id,
        company_id=user.company_id,
        event_data={  # never the password
            "user_id": str(user.id),
            "email": user.email,
            "role": user.role.value,
            "company_id": str(user.company_id) if user.company_id else None,
            "company_name": company.name if company else None,
        },
    )
    db.commit()
    db.refresh(user)
    return _user_response(user, _company_names(db))


@router.patch(
    "/users/{user_id}",
    response_model=UserAdminResponse,
    summary="Change a user's role, company or active flag",
    description=(
        "Partial update of `role`, `company_id` and/or `is_active`. A platform admin cannot "
        "change their own role or deactivate themselves (this also guarantees a platform admin "
        "always remains). Changing a user's company or role invalidates their existing tokens "
        "(they must sign in again). There is no self-service signup and no password-reset "
        "endpoint. Platform admin only."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        **_PLATFORM_ONLY,
        400: {"description": "Attempted to change your own role or deactivate yourself."},
        404: {"description": "No such user (or company)."},
        422: {"description": "Company/role combination is invalid."},
    },
)
def update_user(
    user_id: uuid.UUID,
    payload: UserAdminUpdate,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_system_db),
) -> UserAdminResponse:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    if user.id == admin.id:
        if payload.role is not None and payload.role != user.role:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't change your own role.")
        if payload.is_active is False:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "You can't deactivate your own account.")

    new_role = payload.role if payload.role is not None else user.role
    if "company_id" in payload.model_fields_set:
        new_company_id = payload.company_id
    else:
        new_company_id = user.company_id
    if new_role == UserRole.platform_admin:
        new_company_id = None
    else:
        _require_active_company(db, new_company_id)

    changes: dict[str, dict[str, Any]] = {}
    if new_role != user.role:
        changes["role"] = {"from": user.role.value, "to": new_role.value}
        user.role = new_role
    if new_company_id != user.company_id:
        changes["company_id"] = {
            "from": str(user.company_id) if user.company_id else None,
            "to": str(new_company_id) if new_company_id else None,
        }
        user.company_id = new_company_id
    if payload.is_active is not None and payload.is_active != user.is_active:
        changes["is_active"] = {"from": user.is_active, "to": payload.is_active}
        user.is_active = payload.is_active
    if changes:
        record_event(
            db,
            "user_updated",
            actor_user_id=admin.id,
            company_id=user.company_id,
            event_data={"user_id": str(user.id), "email": user.email, "changes": changes},
        )
        db.commit()
        db.refresh(user)
    return _user_response(user, _company_names(db))


@router.post(
    "/users/{user_id}/reset-password",
    response_model=UserAdminResponse,
    summary="Reset user password (platform admin only)",
    description=(
        "Resets the password for any user account. The new password is stored hashed. "
        "Only platform admins have access to this action. Action is logged in the audit trail."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        **_PLATFORM_ONLY,
        404: {"description": "No such user."},
    },
)
def reset_user_password(
    user_id: uuid.UUID,
    payload: UserPasswordReset,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_system_db),
) -> UserAdminResponse:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    user.hashed_password = hash_password(payload.password)
    record_event(
        db,
        "user_password_reset",
        actor_user_id=admin.id,
        company_id=user.company_id,
        event_data={"user_id": str(user.id), "email": user.email},
    )
    db.commit()
    db.refresh(user)
    return _user_response(user, _company_names(db))
