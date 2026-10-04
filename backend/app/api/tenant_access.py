"""
Binding a request to the company whose data it touches, and auditing
platform-admin support access.

Company users are already bound to their own company by `get_tenant_db`
(app/api/auth.py): a case id from another company is simply not found (404),
by the application-layer filter and by Row-Level Security alike.

Platform admins belong to no company. A route they may use resolves which
company the requested resource belongs to through the platform session, binds
the request's data session to THAT company (so everything after it is still
confined to one company, by both layers), and writes a
`platform_admin_access` audit_log row — e.g.
"Platform admin ops@fddt.io viewed Acme Ltd / Case CASE-1A2B3C4D".

Those rows are PLATFORM-ONLY. They are written through the platform session
with company_id = NULL (the target company is in event_data.company_id), so
no company session can ever read them — Row-Level Security hides NULL-company
rows from the app role, and the company-facing audit queries also exclude the
event type explicitly. Company users have no way to see that platform access
happened; platform admins see every single access (no de-duplication by
default — PLATFORM_ACCESS_AUDIT_DEDUP_SECONDS=0).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import AuthContext, get_auth_context, get_tenant_db
from app.core.config import settings
from app.db import tenancy
from app.db.session import get_system_db
from app.models.audit_log import AuditLog
from app.models.base import utcnow
from app.models.case import Case
from app.models.company import Company
from app.models.user import User
from app.services.audit_service import record_event

PLATFORM_ACCESS_EVENT = "platform_admin_access"


def _describe(method: str) -> str:
    return "viewed" if method in ("GET", "HEAD") else "changed"


def record_platform_access(
    system_db: Session,
    admin: User,
    company: Company,
    request: Request,
    *,
    resource: str,
    case: Case | None = None,
) -> None:
    """Write (and commit) the platform-only audit row for a platform admin's
    access to a company's data, through the PLATFORM session (company_id
    NULL, target company in event_data). Every access is recorded; an
    optional de-duplication window (PLATFORM_ACCESS_AUDIT_DEDUP_SECONDS > 0)
    collapses identical reads by the same admin."""
    state = tenancy.snapshot(system_db)
    tenancy.bind_platform(system_db)
    try:
        _record_platform_access(system_db, admin, company, request, resource=resource, case=case)
    finally:
        tenancy.restore(system_db, state)


def _record_platform_access(
    system_db: Session,
    admin: User,
    company: Company,
    request: Request,
    *,
    resource: str,
    case: Case | None,
) -> None:
    path = request.url.path
    verb = _describe(request.method)
    window = settings.platform_access_audit_dedup_seconds
    if verb == "viewed" and window > 0:
        recent = system_db.execute(
            select(AuditLog.event_data)
            .where(
                AuditLog.event_type == PLATFORM_ACCESS_EVENT,
                AuditLog.actor_user_id == admin.id,
                AuditLog.company_id.is_(None),
                AuditLog.created_at >= utcnow() - timedelta(seconds=window),
            )
        ).scalars().all()
        if any(
            (data or {}).get("path") == path
            and (data or {}).get("method") == request.method
            and (data or {}).get("company_id") == str(company.id)
            for data in recent
        ):
            return

    target = f"{company.name} / Case {case.case_number}" if case is not None else f"{company.name} / {resource}"
    record_event(
        system_db,
        PLATFORM_ACCESS_EVENT,
        company_id=None,  # platform-only: invisible to every company session
        case_id=case.id if case is not None else None,
        actor_user_id=admin.id,
        event_data={
            "summary": f"Platform admin {admin.email} {verb} {target}",
            "platform_admin": True,
            "admin_email": admin.email,
            "admin_name": admin.full_name,
            "company_id": str(company.id),
            "company_name": company.name,
            "resource": resource,
            "case_number": case.case_number if case is not None else None,
            "method": request.method,
            "path": path,
        },
    )
    system_db.commit()


def _load_company(system_db: Session, company_id: uuid.UUID) -> Company:
    state = tenancy.snapshot(system_db)
    tenancy.bind_platform(system_db)
    try:
        company = system_db.get(Company, company_id)
    finally:
        tenancy.restore(system_db, state)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    return company


# ---------------------------------------------------------------------------
# Case-scoped routes (/cases/{case_id}/...)
# ---------------------------------------------------------------------------


@dataclass
class CaseScope:
    db: Session
    ctx: AuthContext
    case_id: uuid.UUID
    company_id: uuid.UUID


def get_case_scope(
    case_id: uuid.UUID,
    request: Request,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_tenant_db),
    system_db: Session = Depends(get_system_db),
) -> CaseScope:
    """Confine the request to the company that owns `case_id`.

    Company user: the session is already bound to their company; a case of
    another company is a 404 here (never a silent empty result).
    Platform admin: resolve the case's company through the platform session,
    bind to it, and audit the access."""
    if not ctx.is_platform_admin:
        exists = db.execute(
            select(Case.id).where(Case.id == case_id, Case.company_id == ctx.company_id)
        ).first()
        if exists is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Case not found")
        return CaseScope(db=db, ctx=ctx, case_id=case_id, company_id=ctx.company_id)

    state = tenancy.snapshot(system_db)
    tenancy.bind_platform(system_db)
    try:
        company_id = system_db.execute(
            select(Case.company_id).where(Case.id == case_id)
        ).scalar_one_or_none()
    finally:
        tenancy.restore(system_db, state)
    if company_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Case not found")

    company = _load_company(system_db, company_id)
    tenancy.bind_company(db, company_id)
    case = db.execute(
        select(Case).where(Case.id == case_id, Case.company_id == company_id)
    ).scalar_one()
    record_platform_access(system_db, ctx.user, company, request, resource="case", case=case)
    return CaseScope(db=db, ctx=ctx, case_id=case_id, company_id=company_id)


# ---------------------------------------------------------------------------
# Company-scoped routes that aren't about one case (case list, settings, audit)
# ---------------------------------------------------------------------------


@dataclass
class CompanyScope:
    db: Session
    ctx: AuthContext
    company_id: uuid.UUID


def company_scope(resource: str):
    """Dependency factory. Company user: their own company (a `company_id`
    query parameter naming any other company is a 404). Platform admin: the
    `company_id` query parameter is required, and the access is audited."""

    def dependency(
        request: Request,
        company_id: uuid.UUID | None = Query(
            default=None,
            description="Platform admins only (required for them): the company to act on. "
            "Company users are always confined to their own company.",
        ),
        ctx: AuthContext = Depends(get_auth_context),
        db: Session = Depends(get_tenant_db),
        system_db: Session = Depends(get_system_db),
    ) -> CompanyScope:
        if not ctx.is_platform_admin:
            if company_id is not None and company_id != ctx.company_id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
            return CompanyScope(db=db, ctx=ctx, company_id=ctx.company_id)
        if company_id is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Platform admins must choose a company (company_id query parameter).",
            )
        company = _load_company(system_db, company_id)
        tenancy.bind_company(db, company_id)
        record_platform_access(system_db, ctx.user, company, request, resource=resource)
        return CompanyScope(db=db, ctx=ctx, company_id=company_id)

    return dependency
