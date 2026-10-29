"""
Audit history — the read side of the append-only `audit_log` table (System Specification
section 3.5). This is the same table every check task, case action and
settings change already writes to via `record_event`; this endpoint adds no
events and no parallel log, it only exposes them.

Tenancy: a company reviewer sees only their own company's log, and NOTHING a
platform admin did — neither their support access (`platform_admin_access`)
nor their changes (users created/updated, passwords reset, issuer registry or
risk-rule edits). Those rows are platform-only: written with company_id NULL
(the company they concern is in event_data.company_id), so RLS hides them
from every company session, and the company-facing queries also exclude
anything flagged `platform_admin`. A platform admin passes `company_id` to
read one company's log, which then INCLUDES the platform-only rows about that
company (that read is itself audited), or omits it to read the platform-level
log (companies and users created, usage reconciliations, and every
platform-admin action).

(A submitter sees their own case's timeline through GET /cases/{id}/audit-log.)
"""
import csv
import io
import json
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
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
    resource: str = "audit log",
) -> tuple[Session, uuid.UUID | None]:
    """(session to read with, company whose log it is — None = platform log).
    A platform admin always reads through the platform session (the
    company's log view also shows the platform-only access rows about it)."""
    if not ctx.is_platform_admin:
        tenancy.include_deleted_cases(db)
        return db, ctx.company_id
    if company_id is None:
        return tenancy.bind_platform(system_db), None
    company = _load_company(system_db, company_id)
    record_platform_access(system_db, ctx.user, company, request, resource=resource)
    tenancy.bind_platform(system_db)
    return system_db, company_id


def _scope_filter(ctx: AuthContext, scope_company: uuid.UUID | None):
    if not ctx.is_platform_admin:
        # Company users: their company's rows, plus case_deleted / case_restored,
        # but never platform support access (PLATFORM_ACCESS_EVENT) or internal platform admin edits.
        return and_(
            AuditLog.company_id == scope_company,
            AuditLog.event_type != PLATFORM_ACCESS_EVENT,
            or_(
                AuditLog.event_data.is_(None),
                func.coalesce(AuditLog.event_data["platform_admin"].as_boolean(), False).is_(False),
                AuditLog.event_type.in_(("case_deleted", "case_restored")),
            ),
        )
    if scope_company is None:
        return or_(
            AuditLog.company_id.is_(None),
            AuditLog.event_type.in_(("case_deleted", "case_restored")),
        )
    # Platform-only rows about this company (support access, user and
    # settings changes) sit at company_id NULL with the company in event_data.
    about = and_(
        AuditLog.company_id.is_(None),
        AuditLog.event_data["company_id"].as_string() == str(scope_company),
    )
    return or_(AuditLog.company_id == scope_company, about)


def _events_query(
    ctx: AuthContext,
    scope_company: uuid.UUID | None,
    event_type: str | None,
    case_id: uuid.UUID | None,
    q: str | None,
):
    """The filtered event query shared by the paged list and the export."""
    base = (
        select(AuditLog, Case.case_number, Case.deleted_at, User, Document.original_filename)
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
    return base


def _clean_event_data(entry: AuditLog, is_platform_admin: bool = False) -> dict | None:
    if not entry.event_data:
        return None
    data = dict(entry.event_data)
    if not is_platform_admin:
        data.pop("admin_email", None)
        data.pop("admin_name", None)
    return data


def _actor_name(entry: AuditLog, actor: User | None, is_platform_admin: bool = False) -> str | None:
    if not is_platform_admin and (
        entry.event_type in ("case_deleted", "case_restored")
        or (entry.event_data or {}).get("platform_admin")
    ):
        return "Platform Administrator"
    if actor is not None:
        return actor.full_name or actor.email
    data = entry.event_data or {}
    # Platform-only rows carry the admin's identity themselves; only a
    # platform admin's session ever reads those.
    return data.get("admin_name") or data.get("admin_email")


def _actor_email(entry: AuditLog, actor: User | None, is_platform_admin: bool = False) -> str | None:
    if not is_platform_admin and (
        entry.event_type in ("case_deleted", "case_restored")
        or (entry.event_data or {}).get("platform_admin")
    ):
        return None
    return actor.email if actor else (entry.event_data or {}).get("admin_email")


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
    base = _events_query(ctx, scope_company, event_type, case_id, q)

    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    rows = session.execute(
        base.order_by(AuditLog.created_at.desc(), AuditLog.id).limit(limit).offset(offset)
    ).all()

    return AuditPage(
        total=total,
        items=[
            AuditEventResponse(
                id=entry.id,
                created_at=entry.created_at,
                event_type=entry.event_type,
                actor_name=_actor_name(entry, actor, is_platform_admin=ctx.is_platform_admin),
                actor_email=_actor_email(entry, actor, is_platform_admin=ctx.is_platform_admin),
                case_id=entry.case_id,
                case_number=case_number or (entry.event_data or {}).get("case_number"),
                document_id=entry.document_id,
                document_filename=filename,
                event_data=_clean_event_data(entry, is_platform_admin=ctx.is_platform_admin),
                case_deleted=bool(case_deleted_at is not None),
            )
            for entry, case_number, case_deleted_at, actor, filename in rows
        ],
    )


# ---------------------------------------------------------------------------
# Export (every matching event, any number, one streamed CSV)
# ---------------------------------------------------------------------------

_EXPORT_HEADER = [
    "Timestamp (UTC)", "Event type", "Actor", "Actor email", "Case", "Document", "Summary", "All details (JSON)",
]
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell(value) -> str:
    """Text for one CSV cell. A value that a spreadsheet would run as a
    formula is prefixed with ' (case data comes from uploaded documents)."""
    if value is None:
        return ""
    text = str(value)
    return f"'{text}" if text.startswith(_FORMULA_START) else text


def _csv_line(values: list) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow([_cell(v) for v in values])
    return buffer.getvalue()


def _utc(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()


@router.get(
    "/export",
    summary="Export the audit log as CSV",
    description=(
        "Every event matching the same filters as `GET /audit-log` (`event_type`, `case_id`, `q`, "
        "and `company_id` for platform admins) — no row limit — as one CSV, newest first, streamed "
        "so any size works. UTF-8 with a byte-order mark (Excel opens Arabic correctly); values a "
        "spreadsheet would run as formulas are prefixed with `'`. Same visibility as the list: a "
        "company sees nothing about platform admins. A platform admin's export of a company's log "
        "is recorded once in the platform audit log."
    ),
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/csv": {}}, "description": "The CSV file."},
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires a reviewer (L1 or L2) or platform admin role."},
    },
)
def export_audit_events(
    request: Request,
    event_type: str | None = Query(default=None),
    case_id: uuid.UUID | None = Query(default=None),
    q: str | None = Query(default=None, description="Matches event type, case number, actor name/email"),
    company_id: uuid.UUID | None = Query(default=None, description="Platform admins only."),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_tenant_db),
    system_db: Session = Depends(get_system_db),
) -> StreamingResponse:
    session, scope_company = _audit_session(
        request, company_id, ctx, db, system_db, resource="audit log export"
    )
    stmt = _events_query(ctx, scope_company, event_type, case_id, q).order_by(
        AuditLog.created_at.desc(), AuditLog.id
    )

    def rows() -> Iterator[str]:
        yield "\ufeff" + _csv_line(_EXPORT_HEADER)
        result = session.execute(stmt.execution_options(yield_per=1000))
        for entry, case_number, case_deleted_at, actor, filename in result:
            data = _clean_event_data(entry, is_platform_admin=ctx.is_platform_admin) or {}
            case_num = case_number or (entry.event_data or {}).get("case_number")
            case_display = f"{case_num} (Deleted)" if (case_num and case_deleted_at is not None) else (case_num or "")
            yield _csv_line([
                _utc(entry.created_at),
                entry.event_type,
                _actor_name(entry, actor, is_platform_admin=ctx.is_platform_admin) or "System pipeline",
                _actor_email(entry, actor, is_platform_admin=ctx.is_platform_admin),
                case_display,
                filename,
                data.get("summary") or data.get("reason") or data.get("note"),
                json.dumps(data, ensure_ascii=False, default=str) if data else "",
            ])

    scope = "platform" if ctx.is_platform_admin and scope_company is None else "company"
    filename = f"audit-log-{scope}-{datetime.now(timezone.utc).date().isoformat()}.csv"
    return StreamingResponse(
        rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
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
        tenancy.include_deleted_cases(db)
        session, scope_company = db, ctx.company_id
    stmt = select(AuditLog.event_type).distinct().order_by(AuditLog.event_type)
    stmt = stmt.where(_scope_filter(ctx, scope_company))
    return list(session.execute(stmt).scalars().all())
