"""
Platform-admin API — everything that spans companies. Platform admin only
(403 for every company role), and every route runs on the explicit platform
(RLS-bypassing) session; none of it is reachable from a company session.

  /platform/companies       create / rename / suspend client companies. A new
                            company gets a copy of the platform rule template
                            and default tier thresholds (its own copy to tune).
  /platform/risk-rule-templates
                            the default rule set new companies start from.
  /platform/usage           billing & usage, ONE ROW PER COMPANY for a period
                            (never a mixed list of cases) — read from the
                            incrementally-maintained counters.
  /platform/usage/reconcile recompute the counters from source data now (the
                            same job runs nightly).
  /platform/queues          processing-queue depth, wait times and rate-limit
                            state.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import require_platform_admin
from app.core.config import settings
from app.db.session import get_system_db
from app.models.company import Company
from app.models.risk_rule_template import RiskRuleTemplate
from app.models.user import User
from app.services.audit_service import record_event
from app.schemas.settings import RiskRuleCreate
from app.services import risk_rule_catalog as catalog
from app.services.risk_rule_seed import ensure_rule_templates, seed_risk_rules
from app.services.usage_service import reconcile_usage, usage_by_company

router = APIRouter(
    prefix="/platform",
    tags=["platform"],
    dependencies=[Depends(require_platform_admin)],
)

_RESPONSES = {
    401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
    403: {"description": "Requires the platform admin role."},
}


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------


class CompanyResponse(BaseModel):
    id: uuid.UUID
    name: str
    is_active: bool
    created_at: datetime
    user_count: int
    max_file_size_mb: int = Field(description="Largest document this company may upload, in MB.")
    max_zip_size_mb: int = Field(description="Largest bulk-upload zip this company may upload, in MB.")


# No upper ceiling: how much capacity a company gets is the platform admin's
# call. Only a positive whole number of MB is required.
_FILE_LIMIT = Field(default=None, ge=1, description="Max document size in MB (per company).")
_ZIP_LIMIT = Field(default=None, ge=1, description="Max bulk-upload zip size in MB (per company).")


class CompanyCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    # Omitted: the platform defaults (DEFAULT_MAX_FILE_SIZE_MB / _ZIP_SIZE_MB),
    # stored on the row now — never re-derived later.
    max_file_size_mb: int | None = _FILE_LIMIT
    max_zip_size_mb: int | None = _ZIP_LIMIT

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) < 2:
            raise ValueError("Enter the company's name.")
        return v


class CompanyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    is_active: bool | None = None
    max_file_size_mb: int | None = _FILE_LIMIT
    max_zip_size_mb: int | None = _ZIP_LIMIT


def _company_response(db: Session, company: Company) -> CompanyResponse:
    count = db.execute(select(func.count()).select_from(User).where(User.company_id == company.id)).scalar_one()
    return CompanyResponse(
        id=company.id, name=company.name, is_active=company.is_active,
        created_at=company.created_at, user_count=int(count),
        max_file_size_mb=company.max_file_size_mb, max_zip_size_mb=company.max_zip_size_mb,
    )


@router.get("/companies", response_model=list[CompanyResponse], summary="List companies", responses=_RESPONSES)
def list_companies(db: Session = Depends(get_system_db)) -> list[CompanyResponse]:
    counts = dict(
        db.execute(
            select(User.company_id, func.count()).where(User.company_id.is_not(None)).group_by(User.company_id)
        ).all()
    )
    return [
        CompanyResponse(
            id=c.id, name=c.name, is_active=c.is_active, created_at=c.created_at,
            user_count=int(counts.get(c.id, 0)),
            max_file_size_mb=c.max_file_size_mb, max_zip_size_mb=c.max_zip_size_mb,
        )
        for c in db.execute(select(Company).order_by(Company.name)).scalars().all()
    ]


@router.post(
    "/companies",
    response_model=CompanyResponse,
    status_code=201,
    summary="Create a company",
    description=(
        "Creates a client company and copies every active platform rule template into its own "
        "risk rules, plus default tier thresholds (its own independent copy, which its Reviewer "
        "L2s can then tune; later template edits don't affect it). The issuer "
        "registry starts empty. Users are added separately (Settings > Users)."
    ),
    responses={**_RESPONSES, 409: {"description": "A company with this name already exists."}},
)
def create_company(
    payload: CompanyCreate,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_system_db),
) -> CompanyResponse:
    if db.execute(select(Company.id).where(func.lower(Company.name) == payload.name.lower())).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "A company with this name already exists.")
    company = Company(
        name=payload.name,
        is_active=True,
        max_file_size_mb=payload.max_file_size_mb or settings.default_max_file_size_mb,
        max_zip_size_mb=payload.max_zip_size_mb or settings.default_max_zip_size_mb,
    )
    db.add(company)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A company with this name already exists.")
    rules = seed_risk_rules(db, company.id)
    record_event(
        db,
        "company_created",
        actor_user_id=admin.id,
        event_data={
            "company_id": str(company.id), "name": company.name, "seeded_risk_rules": rules,
            "max_file_size_mb": company.max_file_size_mb, "max_zip_size_mb": company.max_zip_size_mb,
        },
    )
    db.commit()
    db.refresh(company)
    return _company_response(db, company)


@router.patch(
    "/companies/{company_id}",
    response_model=CompanyResponse,
    summary="Rename, suspend/reactivate, or change a company's upload limits",
    description=(
        "`is_active=false` suspends the company: its users can no longer sign in, and existing "
        "tokens stop working on the next request. Data is kept. `max_file_size_mb` / "
        "`max_zip_size_mb` change the company's upload limits (any positive whole number of MB; "
        "no upper ceiling), effective on its next upload with no new sign-in. Every change is "
        "written to the platform audit log as `company_updated` with old → new values."
    ),
    responses={**_RESPONSES, 404: {"description": "No such company."}, 409: {"description": "Name taken."}},
)
def update_company(
    company_id: uuid.UUID,
    payload: CompanyUpdate,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_system_db),
) -> CompanyResponse:
    company = db.get(Company, company_id)
    if company is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Company not found")
    changes = {}
    if payload.name is not None:
        name = " ".join(payload.name.split())
        if name != company.name:
            clash = db.execute(
                select(Company.id).where(func.lower(Company.name) == name.lower(), Company.id != company.id)
            ).first()
            if clash:
                raise HTTPException(status.HTTP_409_CONFLICT, "A company with this name already exists.")
            changes["name"] = {"from": company.name, "to": name}
            company.name = name
    if payload.is_active is not None and payload.is_active != company.is_active:
        changes["is_active"] = {"from": company.is_active, "to": payload.is_active}
        company.is_active = payload.is_active
    # Upload limits: effective on the company's next upload request (they
    # are read fresh each time — app/services/upload_limits.py).
    for field in ("max_file_size_mb", "max_zip_size_mb"):
        new = getattr(payload, field)
        if new is not None and new != getattr(company, field):
            changes[field] = {"from": getattr(company, field), "to": new}
            setattr(company, field, new)
    if changes:
        record_event(
            db,
            "company_updated",
            actor_user_id=admin.id,
            event_data={"company_id": str(company.id), "name": company.name, "changes": changes},
        )
        db.commit()
        db.refresh(company)
    return _company_response(db, company)


# ---------------------------------------------------------------------------
# Risk-rule templates (the default rule set every NEW company starts from)
# ---------------------------------------------------------------------------


class RuleTemplateResponse(BaseModel):
    id: uuid.UUID
    rule_id: str
    category: str
    check_type: str
    condition: dict
    weight: float
    severity: str
    reason_template: str
    is_active: bool
    updated_at: datetime


class RuleTemplateUpdate(BaseModel):
    weight: float | None = Field(default=None, ge=-100, le=100)
    severity: Literal["low", "medium", "high"] | None = None
    reason_template: str | None = Field(default=None, min_length=3, max_length=1024)
    is_active: bool | None = None


def _template_response(t: RiskRuleTemplate) -> RuleTemplateResponse:
    return RuleTemplateResponse(
        id=t.id, rule_id=t.rule_id, category=t.category, check_type=t.check_type, condition=t.condition,
        weight=t.weight, severity=t.severity, reason_template=t.reason_template, is_active=t.is_active,
        updated_at=t.updated_at,
    )


@router.get(
    "/risk-rule-templates",
    response_model=list[RuleTemplateResponse],
    summary="List the platform risk-rule template",
    description=(
        "The default rule set copied into every NEW company's own risk rules at creation time. "
        "Editing it never changes companies that already exist."
    ),
    responses=_RESPONSES,
)
def list_rule_templates(db: Session = Depends(get_system_db)) -> list[RuleTemplateResponse]:
    ensure_rule_templates(db)
    db.commit()
    rows = db.execute(
        select(RiskRuleTemplate).order_by(RiskRuleTemplate.category, RiskRuleTemplate.rule_id)
    ).scalars().all()
    return [_template_response(t) for t in rows]


@router.post(
    "/risk-rule-templates",
    response_model=RuleTemplateResponse,
    status_code=201,
    summary="Add a rule to the platform template",
    description="Same rule builder as a company's Add-rule form. Applies to companies created from now on.",
    responses={**_RESPONSES, 409: {"description": "A template rule with this rule_id already exists."}},
)
def create_rule_template(
    payload: RiskRuleCreate,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_system_db),
) -> RuleTemplateResponse:
    ensure_rule_templates(db)
    if db.execute(select(RiskRuleTemplate.id).where(RiskRuleTemplate.rule_id == payload.rule_id)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, f"A template rule called '{payload.rule_id}' already exists.")
    check_type, condition = catalog.build_condition(
        payload.match, check_type=payload.check_type, finding=payload.finding,
        severity_in=payload.severity_in, sub_check=payload.sub_check, field_name=payload.field_name,
        signature_result=payload.signature_result,
    )
    template = RiskRuleTemplate(
        rule_id=payload.rule_id, category=payload.category, check_type=check_type, condition=condition,
        weight=payload.weight, severity=payload.severity, reason_template=payload.reason_template,
        is_active=payload.is_active, updated_by=admin.id,
    )
    db.add(template)
    db.flush()
    record_event(
        db,
        "risk_rule_template_created",
        actor_user_id=admin.id,
        event_data={"rule_id": template.rule_id, "weight": template.weight, "severity": template.severity,
                    "is_active": template.is_active, "condition": condition},
    )
    db.commit()
    db.refresh(template)
    return _template_response(template)


@router.patch(
    "/risk-rule-templates/{rule_id}",
    response_model=RuleTemplateResponse,
    summary="Edit a platform template rule",
    description=(
        "Changes weight, severity, wording or active flag. Existing companies keep their own copies "
        "unchanged; only companies created afterwards start from the new values."
    ),
    responses={**_RESPONSES, 404: {"description": "No such template rule."}},
)
def update_rule_template(
    rule_id: str,
    payload: RuleTemplateUpdate,
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_system_db),
) -> RuleTemplateResponse:
    template = db.execute(select(RiskRuleTemplate).where(RiskRuleTemplate.rule_id == rule_id)).scalar_one_or_none()
    if template is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Template rule not found")
    changes = {}
    for name in ("weight", "severity", "reason_template", "is_active"):
        new = getattr(payload, name)
        if new is not None and new != getattr(template, name):
            changes[name] = {"from": getattr(template, name), "to": new}
            setattr(template, name, new)
    if changes:
        template.updated_by = admin.id
        record_event(
            db, "risk_rule_template_updated", actor_user_id=admin.id,
            event_data={"rule_id": rule_id, "changes": changes},
        )
        db.commit()
        db.refresh(template)
    return _template_response(template)


# ---------------------------------------------------------------------------
# Billing & usage
# ---------------------------------------------------------------------------

Period = Literal["this_month", "last_month", "last_30_days", "this_year", "all_time", "custom"]


class CompanyUsageRow(BaseModel):
    company_id: uuid.UUID
    company_name: str
    is_active: bool
    cases_created: int
    documents_uploaded: int
    files_stored: int
    storage_bytes: int
    total_storage_bytes: int
    total_files_stored: int


class UsageTotals(BaseModel):
    cases_created: int
    documents_uploaded: int
    files_stored: int
    storage_bytes: int
    total_storage_bytes: int
    total_files_stored: int


class UsageResponse(BaseModel):
    period: Period
    start: date | None = Field(description="First UTC day included (null = from the beginning).")
    end: date | None = Field(description="Last UTC day included (null = up to today).")
    companies: list[CompanyUsageRow]
    totals: UsageTotals


def _period_range(period: Period, start: date | None, end: date | None) -> tuple[date | None, date | None]:
    today = datetime.now(timezone.utc).date()
    if period == "this_month":
        return today.replace(day=1), today
    if period == "last_month":
        last_day = today.replace(day=1) - timedelta(days=1)
        return last_day.replace(day=1), last_day
    if period == "last_30_days":
        return today - timedelta(days=29), today
    if period == "this_year":
        return today.replace(month=1, day=1), today
    if period == "all_time":
        return None, None
    if start is None or end is None:
        raise HTTPException(422, "A custom period needs both start and end.")
    if start > end:
        raise HTTPException(422, "The start date must be on or before the end date.")
    return start, end


@router.get(
    "/usage",
    response_model=UsageResponse,
    summary="Billing & usage by company",
    description=(
        "One row per company for the period: cases created, documents uploaded, files stored "
        "(documents + generated reports) and bytes stored during the period, plus each "
        "company's current all-time storage. Read from incrementally-maintained daily counters "
        "(company_usage_stats), not live scans of the case tables; a nightly job reconciles them "
        "against the source data. Periods use UTC days."
    ),
    responses=_RESPONSES,
)
def get_usage(
    period: Period = Query(default="this_month"),
    start: date | None = Query(default=None, description="custom period only"),
    end: date | None = Query(default=None, description="custom period only"),
    db: Session = Depends(get_system_db),
) -> UsageResponse:
    range_start, range_end = _period_range(period, start, end)
    rows = usage_by_company(db, start=range_start, end=range_end)
    companies = [CompanyUsageRow(**row.__dict__) for row in rows]
    totals = UsageTotals(
        **{
            field: sum(getattr(r, field) for r in companies)
            for field in UsageTotals.model_fields
        }
    )
    return UsageResponse(period=period, start=range_start, end=range_end, companies=companies, totals=totals)


class ReconcileResponse(BaseModel):
    rows_checked: int
    rows_corrected: int
    drift: list[dict]


@router.post(
    "/usage/reconcile",
    response_model=ReconcileResponse,
    summary="Reconcile usage counters now",
    description=(
        "Recomputes every company's daily counters from the cases/documents/reports tables and "
        "corrects any drift (the same job runs nightly). Returns what was corrected; a "
        "correction is also written to the platform audit log."
    ),
    responses=_RESPONSES,
)
def reconcile_now(
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_system_db),
) -> ReconcileResponse:
    result = reconcile_usage(db)
    if result.rows_corrected:
        record_event(
            db,
            "usage_stats_reconciled",
            actor_user_id=admin.id,
            event_data={
                "trigger": "manual",
                "rows_checked": result.rows_checked,
                "rows_corrected": result.rows_corrected,
                "drift": result.drift[:100],
            },
        )
    db.commit()
    return ReconcileResponse(rows_checked=result.rows_checked, rows_corrected=result.rows_corrected, drift=result.drift)


# ---------------------------------------------------------------------------
# Processing queues
# ---------------------------------------------------------------------------


@router.get(
    "/queues",
    summary="Processing queue monitor",
    description=(
        "Per queue (extraction / vision / forensics): tasks waiting and running, age of the "
        "oldest waiting task, average / p95 / max wait over the window, average run time, the "
        "rate limit in force and the limiter's counters (calls throttled, Azure 429s), and the "
        "worker processes consuming it. Wait times are measured from enqueue to start."
    ),
    responses=_RESPONSES,
)
def queue_status(window_minutes: int = Query(default=15, ge=1, le=1440)) -> dict:
    from app.services import queue_monitor

    snap = queue_monitor.snapshot(window_seconds=window_minutes * 60)
    if snap.get("available"):
        workers = queue_monitor.workers_by_queue()
        for q in snap["queues"]:
            q["workers"] = None if workers is None else workers.get(q["queue"], [])
    return snap
