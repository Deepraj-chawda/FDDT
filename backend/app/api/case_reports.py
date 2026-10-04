"""
Per-case PDF report export (see app/services/case_report_service.py).

Company reviewers only, like the other case actions (a platform admin may list
and download existing reports — audited support access — but not generate) — a submitter never sees
a case's risk reasons or forensic findings, which is what the report is
made of. Generating a report adds a new history row each time; earlier
reports stay downloadable.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import require_company_role, require_reader
from app.api.tenant_access import CaseScope, get_case_scope
from app.models.case import Case
from app.models.case_report import CaseReport
from app.models.case_risk_assessment import CaseRiskAssessment
from app.models.user import User, UserRole
from app.schemas.case_report import CaseReportResponse
from app.services.case_report_service import generate_case_report
from app.services.storage_service import (
    StorageOperationError,
    StorageService,
    ensure_company_blob,
    get_storage_service,
)

router = APIRouter(
    prefix="/cases/{case_id}/reports",
    tags=["case-reports"],
)

_reviewer = require_company_role(UserRole.reviewer_l1)
_reader = require_reader(UserRole.reviewer_l1)


def _get_case(scope: CaseScope) -> Case:
    case = scope.db.execute(
        select(Case).where(Case.id == scope.case_id, Case.company_id == scope.company_id)
    ).scalar_one_or_none()
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Case not found")
    return case


def _to_response(
    db: Session, report: CaseReport, storage: StorageService
) -> CaseReportResponse:
    generator = db.get(User, report.generated_by_user_id)
    assessment = (
        db.get(CaseRiskAssessment, report.risk_assessment_id) if report.risk_assessment_id else None
    )
    try:
        ensure_company_blob(report.blob_url, report.company_id)
        url = storage.get_download_url(report.blob_url)
    except StorageOperationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return CaseReportResponse(
        id=report.id,
        case_id=report.case_id,
        generated_by_name=(generator.full_name or generator.email) if generator else None,
        generated_at=report.generated_at,
        file_size_bytes=report.file_size_bytes,
        page_count=report.page_count,
        report_sha256=report.report_sha256,
        risk_tier=assessment.tier.value if assessment else None,
        risk_score=assessment.score if assessment else None,
        download_url=url,
    )


@router.post(
    "",
    response_model=CaseReportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a case report (PDF)",
    description=(
        "Company reviewers only. Renders the per-case forensic report PDF, stores it in Blob "
        "Storage under the company's `companies/{company_id}/cases/{case_id}/reports/` prefix, records a `case_reports` row (with the file's SHA-256) and "
        "returns it with a signed download URL. Every call creates a new report; earlier ones "
        "are kept."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires a reviewer (L1 or L2) or `admin` role."},
        404: {"description": "No such case."},
        502: {"description": "Blob Storage failed while saving or signing the report."},
    },
)
def create_case_report(
    case_id: uuid.UUID,
    actor: User = Depends(_reviewer),
    scope: CaseScope = Depends(get_case_scope),
    storage: StorageService = Depends(get_storage_service),
) -> CaseReportResponse:
    db = scope.db
    case = _get_case(scope)
    try:
        report = generate_case_report(db, case, actor, storage)
    except StorageOperationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return _to_response(db, report, storage)


@router.get(
    "",
    response_model=list[CaseReportResponse],
    summary="List a case's generated reports",
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Requires a reviewer (L1 or L2) or `admin` role."},
        404: {"description": "No such case."},
        502: {"description": "Blob Storage failed while signing a download URL."},
    },
)
def list_case_reports(
    case_id: uuid.UUID,
    _actor: User = Depends(_reader),
    scope: CaseScope = Depends(get_case_scope),
    storage: StorageService = Depends(get_storage_service),
) -> list[CaseReportResponse]:
    """Report history for the case, newest first."""
    db = scope.db
    _get_case(scope)
    reports = db.execute(
        select(CaseReport)
        .where(CaseReport.case_id == case_id, CaseReport.company_id == scope.company_id)
        .order_by(CaseReport.generated_at.desc())
    ).scalars().all()
    return [_to_response(db, r, storage) for r in reports]
