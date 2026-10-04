"""
Celery task: image-tampering detection — Error Level Analysis +
copy-move detection (CLAUDE.md section 3.2; see app/services/forensics/
ela.py and copy_move.py for the actual analysis).

One task runs both checks — not because they're the same check_type
(they're two separate document_checks rows, same as every other check
in this codebase) but because they both need the PDF rendered to page
images first, and that render is the expensive part; doing it once here
and handing the same page list to both check functions avoids rendering
the same document twice (see app/services/forensics/pdf_render.py).

Like app/tasks/metadata_forensics_task.py, this only reads the PDF's own
bytes — no OCR text or classified document_type needed — so it's
enqueued directly from the upload endpoint (app/api/documents.py) and
runs independently/in parallel, not chained after extraction.

PDF-only for now (CLAUDE.md's "starting with PDF documents only" scope
note, same gate as metadata_forensics_task.py) — a non-PDF document is a
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
from app.services.forensics.copy_move import run_copy_move_check
from app.services.forensics.ela import run_ela_check
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


@celery_app.task(name="run_tampering_checks")
def run_tampering_checks(document_id: str, company_id: str | None = None) -> None:
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

            ela_result = run_ela_check(pages)
            copy_move_result = run_copy_move_check(pages)

            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.error_level_analysis,
                status=DocumentCheckStatus.completed,
                result=ela_result,
            )
            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.copy_move_detection,
                status=DocumentCheckStatus.completed,
                result=copy_move_result,
            )
            record_event(
                db,
                "tampering_checks_completed",
                case_id=document.case_id,
                document_id=document.id,
                event_data={
                    "page_count": len(pages),
                    "ela_result": ela_result["result"],
                    "ela_finding_count": len(ela_result["details"]),
                    "copy_move_result": copy_move_result["result"],
                    "copy_move_finding_count": len(copy_move_result["details"]),
                },
            )
        except Exception as exc:  # noqa: BLE001 - a forensics failure must
            # still land as clean "failed" document_checks rows + an
            # audit entry, not an unhandled worker crash. Both check
            # types get a failed row since we can't tell from a shared-
            # render exception which one (if either) would have
            # succeeded on its own.
            error_message = str(exc)[:_ERROR_MESSAGE_MAX_LENGTH]
            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.error_level_analysis,
                status=DocumentCheckStatus.failed,
                error_message=error_message,
            )
            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.copy_move_detection,
                status=DocumentCheckStatus.failed,
                error_message=error_message,
            )
            record_event(
                db,
                "tampering_checks_failed",
                case_id=document.case_id,
                document_id=document.id,
                event_data={"error": error_message},
            )
        db.commit()
        request_case_scoring(document.case_id, document.company_id)
    finally:
        db.close()
