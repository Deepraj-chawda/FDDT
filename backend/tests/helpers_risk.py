"""Shared builders for the risk-scoring / workflow tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.models.audit_log import AuditLog
from app.models.case import Case, CaseStatus, CaseType
from app.models.document import Document, DocumentProcessingStatus
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType

PASS = {"result": "pass", "details": []}

# Every check the pipeline must have finished before a case is scorable.
ALL_PDF_CHECKS = [
    DocumentCheckType.field_validation,
    DocumentCheckType.issuer_verification,
    DocumentCheckType.metadata_forensics,
    DocumentCheckType.error_level_analysis,
    DocumentCheckType.copy_move_detection,
    DocumentCheckType.duplicate_detection,
    DocumentCheckType.visual_inconsistency_review,
]


def finding(name: str, severity: str = "high", description: str = "", page: int = 1, **data) -> dict:
    out: dict[str, Any] = {"finding": name, "severity": severity, "description": description or name, "page": page}
    if data:
        out["data"] = data
    return out


def add_document(
    db,
    case: Case,
    user,
    name: str,
    *,
    checks: dict[DocumentCheckType, dict] | None = None,
    complete: bool = True,
    content_type: str = "application/pdf",
) -> Document:
    """A document whose every required check has a completed row (PASS by
    default); `checks` overrides individual check results."""
    doc = Document(
        case_id=case.id,
        uploaded_by_user_id=user.id,
        original_filename=name,
        blob_storage_path=f"https://fake/{uuid.uuid4()}",
        file_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        content_type=content_type,
        processing_status=DocumentProcessingStatus.complete if complete else DocumentProcessingStatus.processing,
    )
    db.add(doc)
    db.flush()
    if complete:
        overrides = checks or {}
        for check_type in [*ALL_PDF_CHECKS, *[t for t in overrides if t not in ALL_PDF_CHECKS]]:
            db.add(
                DocumentCheck(
                    document_id=doc.id,
                    check_type=check_type,
                    status=DocumentCheckStatus.completed,
                    result=overrides.get(check_type, PASS),
                )
            )
    db.commit()
    return doc


def add_cross_doc_event(db, case: Case) -> None:
    """The audit event the cross-document task writes when it has run."""
    db.add(
        AuditLog(
            case_id=case.id,
            event_type="cross_document_check_completed",
            event_data={"finding_count": 0},
            created_at=datetime.now(timezone.utc),
        )
    )
    db.commit()


def make_case(db, user, *, status: CaseStatus = CaseStatus.submitted, number: str | None = None) -> Case:
    case = Case(
        case_number=number or f"CASE-{uuid.uuid4().hex[:8].upper()}",
        case_type=CaseType.vendor_invoice,
        submitted_by_user_id=user.id,
        status=status,
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    return case
