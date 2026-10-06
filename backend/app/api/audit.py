"""
Audit history — the read side of the append-only `audit_log` table (System Specification
section 3.5). This is the same table every check task, case action and
settings change already writes to via `record_event`; this endpoint adds no
events and no parallel log, it only exposes them.

Tenancy: a company reviewer sees only their own company's log — never the
`platform_admin_access` rows (those are platform-only: company_id NULL, so
RLS hides them, and every company-facing query also excludes the event type).
A platform admin passes `company_id` to read one company's log, which then
INCLUDES the platform-access rows about that company (that read is itself
audited), or omits it to read the platform-level log (companies and users
created, usage reconciliations, and every platform-admin access).

(A submitter sees their own case's timeline through GET /cases/{id}/audit-log.)
"""
import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.api.auth import AuthContext, get_auth_context, get_tenant_db, require_reader
from app.api.tenant_access import PLATFORM_ACCESS_EVENT, _load_company, record_platform_access
from app.db import tenancy
from app.db.session import get_system_db
from app.models.audit_log import AuditLog
from app.models.case import Case
from app.models.document import Document
from app.models.user import User, UserRole
from app.schemas.settings import AuditEventResponse, AuditPage

router = APIRouter(
    prefix="/audit-log",
    tags=["audit"],
    dependencies=[Depends(require_reader(UserRole.reviewer_l1))],
)


def _audit_session(
    request: Request,
    company_id: uuid.UUID | None,
    ctx: AuthContext,
    db: Session,
    system_db: Session,
) -> tuple[Session, uuid.UUID | None]:
    """(session to read with, company whose log it is — None = platform log).
    A platform admin always reads through the platform session (the
    company's log view also shows the platform-only access rows about it)."""
    if not ctx.is_platform_admin:
        return db, ctx.company_id
    if company_id is None:
        return tenancy.bind_platform(system_db), None
    company = _load_company(system_db, company_id)
    record_platform_access(system_db, ctx.user, company, request, resource="audit log")
    tenancy.bind_platform(system_db)
    return system_db, company_id


def _scope_filter(ctx: AuthContext, scope_company: uuid.UUID | None):
    access_about = and_(
        AuditLog.event_type == PLATFORM_ACCESS_EVENT,
        AuditLog.event_data["company_id"].as_string() == str(scope_company),
    )
    if not ctx.is_platform_admin:
        # Company users: their company's rows, and never platform-access rows
        # (also hidden by RLS — this is the application-layer half).
        return and_(AuditLog.company_id == scope_company, AuditLog.event_type != PLATFORM_ACCESS_EVENT)
    if scope_company is None:
        return AuditLog.company_id.is_(None)
    return or_(AuditLog.company_id == scope_company, access_about)


@router.get(
    "",
    response_model=AuditPage,
    summary="Search the audit log",
    description=(
        "Newest-first, paged read of the append-only `audit_log`, confined to the caller's "
        "company. Filters: `event_type`, `case_id` and free-text `q` (event type, case number, "
        "actor name/email). Company reviewers, and platform admins (`company_id` = one "
        "company's log, audited; omitted = the platform-level log)."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires a reviewer (L1 or L2) or platform admin role."},
    },
)
def list_audit_events(
    request: Request,
    event_type: str | None = Query(default=None),
    case_id: uuid.UUID | None = Query(default=None),
    q: str | None = Query(default=None, description="Matches event type, case number, actor name/email"),
    company_id: uuid.UUID | None = Query(default=None, description="Platform admins only."),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_tenant_db),
    system_db: Session = Depends(get_system_db),
) -> AuditPage:
    session, scope_company = _audit_session(request, company_id, ctx, db, system_db)
    base = (
        select(AuditLog, Case.case_number, User, Document.original_filename)
        .outerjoin(Case, Case.id == AuditLog.case_id)
        .outerjoin(User, User.id == AuditLog.actor_user_id)
        .outerjoin(Document, Document.id == AuditLog.document_id)
    )
    base = base.where(_scope_filter(ctx, scope_company))
    if event_type:
        base = base.where(AuditLog.event_type == event_type)
    if case_id:
        base = base.where(AuditLog.case_id == case_id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        base = base.where(
            or_(
                AuditLog.event_type.ilike(like),
                Case.case_number.ilike(like),
                User.email.ilike(like),
                User.full_name.ilike(like),
            )
        )

    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    rows = session.execute(
        base.order_by(AuditLog.created_at.desc(), AuditLog.id).limit(limit).offset(offset)
    ).all()

    def _actor_name(entry: AuditLog, actor: User | None) -> str | None:
        if actor is not None:
            return actor.full_name or actor.email
        data = entry.event_data or {}
        name = data.get("admin_name") or data.get("admin_email")
        if name:
            return name
        # A person acted, but the session can't see their user row: only a
        # platform admin (no company — hidden from a company session by RLS),
        # e.g. "user created" in the company's own log. Say so rather than
        # leaving it blank, which the UI would show as the automated pipeline.
        return "Platform team" if entry.actor_user_id is not None else None

    return AuditPage(
        total=total,
        items=[
            AuditEventResponse(
                id=entry.id,
                created_at=entry.created_at,
                event_type=entry.event_type,
                actor_name=_actor_name(entry, actor),
                actor_email=actor.email if actor else (entry.event_data or {}).get("admin_email"),
                case_id=entry.case_id,
                case_number=case_number,
                document_id=entry.document_id,
                document_filename=filename,
                event_data=entry.event_data,
            )
            for entry, case_number, actor, filename in rows
        ],
    )


@router.get(
    "/event-types",
    response_model=list[str],
    summary="List audit event types",
    description=(
        "Distinct `event_type` values present in the caller's company's audit log (platform "
        "admins: the chosen company's, or the platform log), for the filter dropdown."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires a reviewer (L1 or L2) or platform admin role."},
    },
)
def list_event_types(
    company_id: uuid.UUID | None = Query(default=None, description="Platform admins only."),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_tenant_db),
    system_db: Session = Depends(get_system_db),
) -> list[str]:
    if ctx.is_platform_admin:
        session = tenancy.bind_platform(system_db)
        scope_company = company_id
    else:
        session, scope_company = db, ctx.company_id
    stmt = select(AuditLog.event_type).distinct().order_by(AuditLog.event_type)
    stmt = stmt.where(_scope_filter(ctx, scope_company))
    return list(session.execute(stmt).scalars().all())
