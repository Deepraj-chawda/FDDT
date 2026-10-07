"""
Celery tasks: per-document field validation + issuer verification, and
case-level cross-document consistency checks (SPECIFICATION.md section 3.1/3.2)
— the pipeline stage that follows OCR + classification + extraction
(app/tasks/document_processing.py).

`run_document_checks` is enqueued from app/tasks/document_processing.py
once a document's extraction has succeeded. Once it finishes, it checks
whether every document in the case has now reached a terminal
processing_status (complete or failed) and, if so, enqueues
`run_cross_document_checks` — that's the "once all documents in a case
have finished extraction" trigger from SPECIFICATION.md section 3.1, done here
rather than per-document. The same completion check is also called from
document_processing.py's failure path, so a document that fails
extraction doesn't silently block the case-level check forever.

Like app/tasks/document_processing.py, these tasks run in a separate
Celery worker process with no FastAPI request context, so they use
`SessionLocal` directly rather than the `get_db` FastAPI dependency.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import tenancy
from app.db.session import SessionLocal
from app.models.cross_document_finding import CrossDocumentFinding
from app.models.document import Document, DocumentProcessingStatus
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.services.audit_service import record_event
from app.services.check_store import save_check
from app.services.cross_document_service import find_cross_document_mismatches
from app.services.field_validation_service import validate_fields
from app.services.issuer_service import verify_issuer
from app.services.llm_service import LLMConfigurationError, get_llm_service
from app.services.risk_scoring_service import request_case_scoring
from app.tasks.celery_app import celery_app
from app.tasks.duplicate_check_task import refine_duplicate_check
from app.tasks.tenant import open_task_session, resolve_company_for_case, resolve_company_for_document

_ERROR_MESSAGE_MAX_LENGTH = 4000

# Cross-document consistency only makes sense with 2+ documents to
# compare (SPECIFICATION.md section 3.1: "For every case with 2+ documents").
_MIN_DOCUMENTS_FOR_CROSS_DOCUMENT_CHECK = 2

_TERMINAL_STATUSES = (DocumentProcessingStatus.complete, DocumentProcessingStatus.failed)


def _maybe_enqueue_cross_document_check(
    db: Session, company_id: uuid.UUID, case_id: uuid.UUID
) -> None:
    statuses = db.execute(
        select(Document.processing_status).where(
            Document.case_id == case_id, Document.company_id == company_id
        )
    ).scalars().all()
    if len(statuses) >= _MIN_DOCUMENTS_FOR_CROSS_DOCUMENT_CHECK and all(
        s in _TERMINAL_STATUSES for s in statuses
    ):
        run_cross_document_checks.delay(str(case_id), str(company_id))


def detected_stamps(db: Session, document: Document) -> list[dict] | None:
    """The stamps signature/stamp detection found on `document` — None if it
    has not finished (it runs in parallel, on the vision queue)."""
    detected = detected_regions(db, document)
    return None if detected is None else [d for d in detected if d.get("kind") == "stamp"]


def detected_signatures(db: Session, document: Document) -> list[dict] | None:
    """The signatures detection found on `document` — None if it has not finished."""
    detected = detected_regions(db, document)
    return None if detected is None else [d for d in detected if d.get("kind") == "signature"]


def detected_regions(db: Session, document: Document) -> list[dict] | None:
    """Every signature and stamp region detection found on `document` — None
    if it has not finished (it runs in parallel, on the vision queue)."""
    check = db.execute(
        select(DocumentCheck).where(
            DocumentCheck.document_id == document.id,
            DocumentCheck.company_id == document.company_id,
            DocumentCheck.check_type == DocumentCheckType.signature_stamp_detection,
            DocumentCheck.status == DocumentCheckStatus.completed,
        )
    ).scalars().first()
    if check is None:
        return None
    details = (check.result or {}).get("details")
    detected = details.get("detected") if isinstance(details, dict) else None
    return [d for d in detected or [] if isinstance(d, dict)]


def revalidate_fields(db: Session, document: Document) -> bool:
    """Re-run field validation once signature/stamp detection has finished,
    if validation ran first (its stamp-vs-issuer sub-check was skipped then).
    Returns whether it re-ran. The caller commits."""
    if document.processing_status != DocumentProcessingStatus.complete:
        return False
    existing = db.execute(
        select(DocumentCheck).where(
            DocumentCheck.document_id == document.id,
            DocumentCheck.company_id == document.company_id,
            DocumentCheck.check_type == DocumentCheckType.field_validation,
            DocumentCheck.status == DocumentCheckStatus.completed,
        )
    ).scalars().first()
    if existing is None:
        return False
    result = validate_fields(
        document.extracted_fields or {}, ocr_text=document.ocr_text, stamps=detected_stamps(db, document),
        signatures=detected_signatures(db, document),
    )
    save_check(
        db,
        document=document,
        check_type=DocumentCheckType.field_validation,
        status=DocumentCheckStatus.completed,
        result=result,
    )
    return True


@celery_app.task(name="run_document_checks")
def run_document_checks(document_id: str, company_id: str | None = None) -> None:
    company_uuid = resolve_company_for_document(document_id, company_id)
    if company_uuid is None:
        return  # stale/bad id
    db = open_task_session(SessionLocal, company_uuid)
    try:
        document = db.execute(
            select(Document).where(
                Document.id == uuid.UUID(document_id), Document.company_id == company_uuid
            )
        ).scalar_one_or_none()
        if document is None or document.processing_status != DocumentProcessingStatus.complete:
            # Nothing to validate without successfully extracted fields
            # (see app/tasks/document_processing.py) — and nothing to do
            # for a stale/bad document_id either.
            return

        extracted_fields = document.extracted_fields or {}

        try:
            field_result = validate_fields(
                extracted_fields, ocr_text=document.ocr_text, stamps=detected_stamps(db, document),
                signatures=detected_signatures(db, document),
            )
            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.field_validation,
                status=DocumentCheckStatus.completed,
                result=field_result,
            )
        except Exception as exc:  # noqa: BLE001 - one check's bug shouldn't
            # block the other check below from still running.
            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.field_validation,
                status=DocumentCheckStatus.failed,
                error_message=str(exc)[:_ERROR_MESSAGE_MAX_LENGTH],
            )

        # Duplicate matches judged on the fields now that they exist (the
        # duplicate check runs in parallel with extraction).
        try:
            refine_duplicate_check(db, document)
        except Exception:  # noqa: BLE001 - never block the checks below
            pass

        try:
            try:
                llm_service = get_llm_service()
            except LLMConfigurationError:
                # Not configured in this environment — match_issuer still
                # works fuzzy-only, it just can't fall back to a semantic
                # judgment for cross-script names (see app/services/
                # issuer_service.py).
                llm_service = None
            # Persist field validation and end the transaction first: the
            # issuer match may make an Azure OpenAI call, and no connection
            # should be held while it runs.
            db.commit()
            # Registry match, or "not_checked" when the registry has nothing
            # that could apply — app/services/issuer_service.py.
            result = verify_issuer(
                db,
                document.company_id,
                extracted_fields,
                document_type=document.document_type,
                llm_service=llm_service,
                before_slow_call=db.commit,
            )
            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.issuer_verification,
                status=DocumentCheckStatus.completed,
                result=result,
            )
        except Exception as exc:  # noqa: BLE001 - see comment above.
            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.issuer_verification,
                status=DocumentCheckStatus.failed,
                error_message=str(exc)[:_ERROR_MESSAGE_MAX_LENGTH],
            )

        record_event(
            db,
            "document_checks_completed",
            case_id=document.case_id,
            document_id=document.id,
        )
        db.commit()

        _maybe_enqueue_cross_document_check(db, document.company_id, document.case_id)
        request_case_scoring(document.case_id, document.company_id)
    finally:
        db.close()


@celery_app.task(name="run_cross_document_checks")
def run_cross_document_checks(case_id: str, company_id: str | None = None) -> None:
    company_uuid = resolve_company_for_case(case_id, company_id)
    if company_uuid is None:
        return  # stale/bad id
    db = open_task_session(SessionLocal, company_uuid)
    try:
        case_uuid = uuid.UUID(case_id)
        documents = db.execute(
            select(Document).where(Document.case_id == case_uuid, Document.company_id == company_uuid)
        ).scalars().all()

        completed_documents = [
            d for d in documents if d.processing_status == DocumentProcessingStatus.complete
        ]

        # This task can in principle fire more than once for the same case
        # (e.g. a re-triggered check); cross_document_findings, unlike
        # audit_log, isn't append-only, so clear the previous run's
        # findings before recomputing rather than accumulating stale rows.
        db.query(CrossDocumentFinding).filter(
            CrossDocumentFinding.case_id == case_uuid, CrossDocumentFinding.company_id == company_uuid
        ).delete()

        findings = (
            find_cross_document_mismatches(completed_documents)
            if len(completed_documents) >= _MIN_DOCUMENTS_FOR_CROSS_DOCUMENT_CHECK
            else []
        )
        for finding in findings:
            db.add(CrossDocumentFinding(company_id=company_uuid, case_id=case_uuid, **finding))

        record_event(
            db,
            "cross_document_check_completed",
            case_id=case_uuid,
            event_data={"finding_count": len(findings)},
        )
        db.commit()
        request_case_scoring(case_uuid, company_uuid)
    finally:
        db.close()
