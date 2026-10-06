"""
Per-case PDF report: orchestration + storage (SPECIFICATION.md section 3.5,
"Evidence repository" / reporting). See app/services/case_report_data.py
for what goes in and app/services/case_report_pdf.py for how it is drawn
— including why the annotated page renders in the report do NOT conflict
with the live UI's never-modify-the-original overlay convention.

Generation is synchronous: it downloads each original once (to re-hash it
and render only the pages that carry findings), builds the PDF in memory
and uploads it — a few seconds for a typical case, so there is no Celery
job / poll-until-ready step (the codebase has no bulk/async-export pattern
to match). If reports grow heavy enough to hurt request latency, this
function is the unit to move into a task.

Reports are stored under the case's company prefix
(`companies/{company_id}/cases/{case_id}/reports/`) — separate from the
original case documents (`.../documents/`) — and every generation adds a new
`case_reports` row; nothing is overwritten.
"""
import hashlib
import uuid

import pymupdf
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.case import Case
from app.models.case_report import CaseReport
from app.models.user import User
from app.services.audit_service import record_event
from app.services.case_report_data import collect_report_data
from app.services.case_report_pdf import build_report_pdf
from app.services.risk_scoring_service import latest_assessment
from app.services.storage_service import StorageService, report_blob_path
from app.services.usage_service import record_report_stored


def generate_case_report(
    db: Session, case: Case, generated_by: User, storage: StorageService
) -> CaseReport:
    """Build the report PDF for `case`, store it, and record it. Raises
    StorageOperationError if the upload fails (the caller maps that to a
    502); nothing is recorded in that case."""
    report_id = uuid.uuid4()
    generated_at = utcnow()

    data = collect_report_data(
        db, case, generated_by, storage, report_id=report_id, generated_at=generated_at
    )
    pdf_bytes = build_report_pdf(data)

    with pymupdf.open("pdf", pdf_bytes) as built:
        page_count = built.page_count

    blob_url = storage.upload(
        report_blob_path(case.company_id, case.id, report_id), pdf_bytes, content_type="application/pdf"
    )
    assessment = latest_assessment(db, case.company_id, case.id)
    report = CaseReport(
        id=report_id,
        company_id=case.company_id,
        case_id=case.id,
        generated_by_user_id=generated_by.id,
        generated_at=generated_at,
        blob_url=blob_url,
        file_size_bytes=len(pdf_bytes),
        page_count=page_count,
        report_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        risk_assessment_id=assessment.id if assessment else None,
    )
    db.add(report)
    db.flush()  # the audit_log FK to the case is already satisfied; keep insert order explicit
    record_event(
        db,
        "case_report_generated",
        case_id=case.id,
        actor_user_id=generated_by.id,
        event_data={
            "report_id": str(report_id),
            "page_count": page_count,
            "risk_assessment_id": str(assessment.id) if assessment else None,
            "report_sha256": report.report_sha256,
        },
    )
    record_report_stored(db, case.company_id, len(pdf_bytes))
    db.commit()
    db.refresh(report)
    return report
