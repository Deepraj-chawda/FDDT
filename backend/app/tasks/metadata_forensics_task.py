"""
Celery task: PDF metadata forensics (SPECIFICATION.md section 3.1/3.2 — see
app/services/forensics/metadata_forensics.py for the actual analysis).

Unlike app/tasks/document_processing.py's OCR/classification/extraction
task, this doesn't need the OCR text or a classified document_type —
it only reads the PDF's own bytes/object structure — so it's enqueued
directly from the upload endpoint (app/api/documents.py) right alongside
process_document, running independently/in parallel rather than waiting
for extraction to finish.

PDF-only for now (SPECIFICATION.md's "starting with PDF documents only" scope
note): a non-PDF document is a quiet no-op here, not a flag or error —
this check simply doesn't have anything to say about it yet.
"""
import uuid

from sqlalchemy import select

from app.db import tenancy
from app.db.session import SessionLocal
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.services.audit_service import record_event
from app.services.check_store import save_check
from app.services.forensics.metadata_forensics import analyze_pdf_metadata
from app.services.storage_service import get_storage_service_for_task
from app.services.risk_scoring_service import request_case_scoring
from app.tasks.celery_app import celery_app
from app.tasks.tenant import open_task_session, resolve_company_for_document

_ERROR_MESSAGE_MAX_LENGTH = 4000


def _is_pdf(document: Document) -> bool:
    if document.content_type == "application/pdf":
        return True
    return document.original_filename.lower().endswith(".pdf")


@celery_app.task(name="run_metadata_forensics")
def run_metadata_forensics(document_id: str, company_id: str | None = None) -> None:
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
        if document is None or not _is_pdf(document):
            return
        # End the read transaction before the slow part (download, analysis,
        # external calls) so no database connection is held meanwhile.
        db.commit()

        try:
            storage = get_storage_service_for_task()
            pdf_bytes = storage.download_bytes(document.blob_storage_path)
            analysis = analyze_pdf_metadata(pdf_bytes)
            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.metadata_forensics,
                status=DocumentCheckStatus.completed,
                result=analysis,
            )
            record_event(
                db,
                "metadata_forensics_completed",
                case_id=document.case_id,
                document_id=document.id,
                event_data={"result": analysis["result"], "finding_count": len(analysis["details"])},
            )
        except Exception as exc:  # noqa: BLE001 - a forensics failure must
            # still land as a clean "failed" document_checks row + audit
            # entry, not an unhandled worker crash.
            db.rollback()
            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.metadata_forensics,
                status=DocumentCheckStatus.failed,
                error_message=str(exc)[:_ERROR_MESSAGE_MAX_LENGTH],
            )
            record_event(
                db,
                "metadata_forensics_failed",
                case_id=document.case_id,
                document_id=document.id,
                event_data={"error": str(exc)[:_ERROR_MESSAGE_MAX_LENGTH]},
            )
        db.commit()
        request_case_scoring(document.case_id, document.company_id)
    finally:
        db.close()
