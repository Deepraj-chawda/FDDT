"""
Celery task: image-tampering detection — Error Level Analysis +
copy-move detection (SPECIFICATION.md section 3.2; see app/services/forensics/
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

On a page whose text is vector text drawn over an image (a scan converted
to editable text), both checks see only the image, not the text: a clean
result is reported as "limited", not "pass" (app/services/forensics/
page_structure.py). Such pages get the ghost-content check instead (its own
document_checks row, same task since it reads the same bytes): the erased
background is searched for traces of text deleted or shortened after the
conversion (app/services/forensics/ghost_content.py). Ghost traces inside a
detected signature or stamp are set aside — applied here if detection has
already finished, else when it does (refilter_ghost_content, called from
app/tasks/signature_detection_task.py).

PDF-only for now (SPECIFICATION.md's "starting with PDF documents only" scope
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
from app.services.forensics.ghost_content import add_vision_hints, analyze_ghost_content, exclude_signature_regions
from app.services.forensics.page_structure import analyze_page_structure, limit_pixel_check
from app.services.forensics.pdf_render import render_pdf_pages
from app.services.llm_service import LLMConfigurationError, get_llm_service
from app.services.ocr_service import OCRConfigurationError, OCROperationError, get_ocr_service
from app.services.storage_service import get_storage_service_for_task
from app.services.risk_scoring_service import request_case_scoring
from app.tasks.celery_app import celery_app
from app.tasks.document_checks import detected_regions
from app.tasks.tenant import open_task_session, resolve_company_for_document

_ERROR_MESSAGE_MAX_LENGTH = 4000


def _is_pdf(document: Document) -> bool:
    if document.content_type == "application/pdf":
        return True
    return document.original_filename.lower().endswith(".pdf")


def _crop_reader():
    """OCR for ghost crops (the reviewer's hint and the show-through test):
    best-effort, so a failed read is no reading. None without OCR configured."""
    try:
        service = get_ocr_service()
    except OCRConfigurationError:
        return None

    def read(image: bytes) -> tuple[str, float]:
        try:
            return service.read_image(image)
        except OCROperationError:
            return "", 0.0

    return read


def _erased_content_guesser():
    """The vision model's guess at an erased block (a reviewer's hint);
    None when no LLM is configured."""
    try:
        return get_llm_service().guess_erased_content
    except LLMConfigurationError:
        return None


def refilter_ghost_content(db, document: Document) -> bool:
    """Set aside ghost findings inside the detected signatures/stamps, once
    detection has finished, if the ghost check ran first. Returns whether the
    stored result changed. The caller commits."""
    check = db.execute(
        select(DocumentCheck).where(
            DocumentCheck.document_id == document.id,
            DocumentCheck.company_id == document.company_id,
            DocumentCheck.check_type == DocumentCheckType.ghost_content,
            DocumentCheck.status == DocumentCheckStatus.completed,
        )
    ).scalars().first()
    detected = detected_regions(db, document)
    if check is None or not detected:
        return False
    result = exclude_signature_regions(check.result or {}, detected)
    if result is check.result:
        return False
    save_check(db, document=document, check_type=DocumentCheckType.ghost_content,
               status=DocumentCheckStatus.completed, result=result)
    return True


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

            structure = analyze_page_structure(pdf_bytes)
            ela_result = limit_pixel_check(run_ela_check(pages), structure, "error level analysis")
            copy_move_result = limit_pixel_check(run_copy_move_check(pages), structure, "copy-move detection")

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
            try:
                ghost_result = exclude_signature_regions(
                    analyze_ghost_content(pdf_bytes, read_image=_crop_reader()), detected_regions(db, document)
                )
                guess = _erased_content_guesser()
                if guess is not None:
                    ghost_result = add_vision_hints(ghost_result, pdf_bytes, guess)
                save_check(
                    db,
                    document=document,
                    check_type=DocumentCheckType.ghost_content,
                    status=DocumentCheckStatus.completed,
                    result=ghost_result,
                )
            except Exception as exc:  # noqa: BLE001 - its own failed row; ELA/copy-move stand
                ghost_result = {"result": "failed", "details": []}
                save_check(
                    db,
                    document=document,
                    check_type=DocumentCheckType.ghost_content,
                    status=DocumentCheckStatus.failed,
                    error_message=str(exc)[:_ERROR_MESSAGE_MAX_LENGTH],
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
                    "ghost_content_result": ghost_result["result"],
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
