"""
Celery task: duplicate/near-duplicate detection (SPECIFICATION.md section 3.2 —
see app/services/forensics/duplicate_check.py for the actual analysis).

Like app/tasks/metadata_forensics_task.py and app/tasks/tampering_checks_
task.py, this only reads the PDF's own bytes/rendered pages — no OCR text
or classified document_type needed — so it's enqueued directly from the
upload endpoint (app/api/documents.py) and runs independently/in
parallel, not chained after extraction.

Unlike those two, the underlying check needs the same DB session to read
prior documents' stored page hashes AND to persist this document's own
(app/services/forensics/duplicate_check.py's `run_duplicate_check` does
both) — so this task's own SessionLocal() is passed straight into it,
rather than the check being a pure function over just the rendered pages.

PDF-only for now (SPECIFICATION.md's "starting with PDF documents only" scope
note, same gate as the other forensics tasks) — a non-PDF document is a
quiet no-op here, not a flag or error.
"""
import uuid

from sqlalchemy import select

from app.db import tenancy
from app.db.session import SessionLocal
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.services.audit_service import record_event
from app.services.check_store import save_check
from app.services.forensics.duplicate_check import refine_with_fields, run_duplicate_check
from app.services.forensics.pdf_render import render_pdf_pages
from app.services.storage_service import get_storage_service_for_task
from app.services.risk_scoring_service import request_case_scoring
from app.tasks.celery_app import celery_app
from app.tasks.tenant import open_task_session, resolve_company_for_document

_ERROR_MESSAGE_MAX_LENGTH = 4000


def _is_pdf(document: Document) -> bool:
    if document.content_type == "application/pdf":
        return True
    return document.original_filename.lower().endswith(".pdf")


def refine_duplicate_check(db, document: Document) -> bool:
    """Judge the document's page matches on the extracted fields once both
    sides have them (app/services/forensics/duplicate_check.py
    refine_with_fields). Returns whether the stored result changed. The
    caller commits."""
    check = db.execute(
        select(DocumentCheck).where(
            DocumentCheck.document_id == document.id,
            DocumentCheck.company_id == document.company_id,
            DocumentCheck.check_type == DocumentCheckType.duplicate_detection,
            DocumentCheck.status == DocumentCheckStatus.completed,
        )
    ).scalars().first()
    if check is None or not document.extracted_fields:
        return False
    ids = {
        uuid.UUID(str((d.get("data") or {}).get("matched_document_id")))
        for d in (check.result or {}).get("details") or []
        if isinstance(d, dict) and (d.get("data") or {}).get("matched_document_id")
    }
    if not ids:
        return False
    matched = {
        str(doc_id): fields
        for doc_id, fields in db.execute(
            select(Document.id, Document.extracted_fields).where(
                Document.id.in_(ids), Document.company_id == document.company_id
            )
        ).all()
    }
    result = refine_with_fields(check.result or {}, document.extracted_fields, matched)
    if result is check.result:
        return False
    save_check(db, document=document, check_type=DocumentCheckType.duplicate_detection,
               status=DocumentCheckStatus.completed, result=result)
    return True


@celery_app.task(name="run_duplicate_check")
def run_duplicate_check_task(document_id: str, company_id: str | None = None) -> None:
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
            pages = render_pdf_pages(pdf_bytes)

            analysis = run_duplicate_check(
                db, company_id=document.company_id, document_id=document.id, pages=pages
            )

            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.duplicate_detection,
                status=DocumentCheckStatus.completed,
                result=analysis,
            )
            # Fields already extracted (this check ran last): judge the
            # matches on them now; otherwise document checks do it later.
            try:
                refine_duplicate_check(db, document)
            except Exception:  # noqa: BLE001 - never fail the check over it
                pass
            record_event(
                db,
                "duplicate_check_completed",
                case_id=document.case_id,
                document_id=document.id,
                event_data={
                    "result": analysis["result"],
                    "match_count": len(analysis["details"]),
                },
            )
        except Exception as exc:  # noqa: BLE001 - a forensics failure must
            # still land as a clean "failed" document_checks row + audit
            # entry, not an unhandled worker crash. Rolled back first so
            # any page-hash rows run_duplicate_check already staged
            # before the failure (still pending, never committed) aren't
            # written alongside a check that's being recorded as failed.
            db.rollback()
            error_message = str(exc)[:_ERROR_MESSAGE_MAX_LENGTH]
            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.duplicate_detection,
                status=DocumentCheckStatus.failed,
                error_message=error_message,
            )
            record_event(
                db,
                "duplicate_check_failed",
                case_id=document.case_id,
                document_id=document.id,
                event_data={"error": error_message},
            )
        db.commit()
        request_case_scoring(document.case_id, document.company_id)
    finally:
        db.close()
