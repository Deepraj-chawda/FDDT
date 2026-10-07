"""
Celery task: OCR + classification + extraction for one uploaded document
(SPECIFICATION.md sections 3.1 and 3.6). Triggered automatically right after
upload (see app/api/documents.py) — enqueued via `.delay()`, so the
upload request itself doesn't block on OCR/LLM latency.

Scope is deliberately narrow: this task does NOT run forensics (metadata,
ELA, duplicate detection) or risk scoring — those are separate, later
pipeline stages. It does, on success, enqueue app/tasks/document_checks.py's
`run_document_checks` (field validation + issuer verification), and on
failure it still checks whether that was the case's last document to
reach a terminal status so a stuck extraction doesn't block the
case-level cross-document check forever — see that module for both.

Celery tasks run in a separate worker process with no FastAPI request
context, so this uses `SessionLocal` directly (app/db/session.py) rather
than the `get_db` FastAPI dependency, and the plain (non-HTTP-wrapped)
`_for_task` service accessors rather than the FastAPI DI versions.
"""
import uuid

from sqlalchemy import select

from app.db import tenancy
from app.db.session import SessionLocal
from app.models.document import Document, DocumentProcessingStatus
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.services.audit_service import record_event
from app.services.check_store import save_check
from app.services.extraction_service import classify_and_extract
from app.services.multi_invoice import extract_invoices
from app.services.field_locator_service import attach_field_locations
from app.services.forensics.font_consistency import analyze_font_consistency
from app.services.line_item_parsing import enrich_extracted_fields
from app.services.llm_service import get_llm_service
from app.services.ocr_service import OCRPage, get_ocr_service
from app.services.risk_scoring_service import request_case_scoring
from app.services.storage_service import get_storage_service_for_task
from app.tasks.celery_app import celery_app
from app.tasks.tenant import open_task_session, resolve_company_for_document
from app.tasks.document_checks import _maybe_enqueue_cross_document_check, run_document_checks

# Truncate before writing to `documents.processing_error` — this column
# exists to show a reviewer *what went wrong*, not to hold an entire
# traceback; the full exception is still whatever Celery's own logging
# captures.
_ERROR_MESSAGE_MAX_LENGTH = 4000


def _is_pdf(document: Document) -> bool:
    return document.content_type == "application/pdf" or document.original_filename.lower().endswith(".pdf")


def _detected_signature_regions(db, document: Document) -> list[dict]:
    """Signature/stamp regions, if detection (which runs in parallel, on the
    vision queue) has already finished for this document; else none — OCR's
    own handwriting marks are used regardless."""
    check = db.execute(
        select(DocumentCheck).where(
            DocumentCheck.document_id == document.id,
            DocumentCheck.check_type == DocumentCheckType.signature_stamp_detection,
            DocumentCheck.status == DocumentCheckStatus.completed,
        )
    ).scalars().first()
    details = (check.result or {}).get("details") if check is not None else None
    return list((details or {}).get("detected") or []) if isinstance(details, dict) else []


def _run_font_consistency(db, document: Document, pdf_bytes: bytes | None, ocr_pages: list[OCRPage]) -> None:
    """The font consistency check (app/services/forensics/font_consistency.py).
    Runs here rather than as its own task because scanned pages need this
    task's OCR font estimates. Best effort: a failure is stored as a failed
    check, never fails the extraction."""
    if not _is_pdf(document):
        return
    try:
        if pdf_bytes is None:
            raise RuntimeError("The PDF could not be downloaded for the font consistency check.")
        result = analyze_font_consistency(pdf_bytes, ocr_pages)
        save_check(
            db,
            document=document,
            check_type=DocumentCheckType.font_consistency,
            status=DocumentCheckStatus.completed,
            result=result,
        )
        findings = [d for d in result["details"] if d["finding"] == "font_inconsistency"]
        record_event(
            db,
            "font_consistency_completed",
            case_id=document.case_id,
            document_id=document.id,
            event_data={"result": result["result"], "finding_count": len(findings)},
        )
    except Exception as exc:  # noqa: BLE001
        save_check(
            db,
            document=document,
            check_type=DocumentCheckType.font_consistency,
            status=DocumentCheckStatus.failed,
            error_message=str(exc)[:_ERROR_MESSAGE_MAX_LENGTH],
        )
        record_event(
            db,
            "font_consistency_failed",
            case_id=document.case_id,
            document_id=document.id,
            event_data={"error": str(exc)[:_ERROR_MESSAGE_MAX_LENGTH]},
        )


@celery_app.task(name="process_document")
def process_document(document_id: str, company_id: str | None = None) -> None:
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
        if document is None:
            # Nothing to process — e.g. a stale/bad id. Nothing to write an
            # audit_log row against either (it needs a real document_id FK).
            return

        document.processing_status = DocumentProcessingStatus.processing
        db.commit()

        try:
            storage = get_storage_service_for_task()
            document_url = storage.get_download_url(
                document.blob_storage_path, expires_in_minutes=30
            )

            ocr_result = get_ocr_service().analyze_url(document_url)
            analysis = classify_and_extract(get_llm_service(), ocr_result.text)

            document.document_type = analysis.document_type
            document.ocr_text = ocr_result.text
            extracted_fields = {
                "document_type_confidence": analysis.document_type_confidence,
                "core_fields": analysis.core_fields_as_dict(),
                "additional_fields": [f.model_dump() for f in analysis.additional_fields],
                # Numbers as printed, for the arithmetic checks in
                # app/services/field_validation_service.py.
                "line_items": [item.model_dump() for item in analysis.line_items],
                "amount_in_words": analysis.amount_in_words.model_dump(),
            }
            # Where on the page each value was read from — stored on the field
            # as a normalized `bounding_box` so field-level exceptions can be
            # highlighted (see app/services/field_locator_service.py). Best
            # effort: failing to locate never fails the extraction itself.
            try:
                fields_located = attach_field_locations(ocr_result.pages, extracted_fields)
            except Exception:  # noqa: BLE001
                fields_located = 0
            pdf_bytes = None
            if _is_pdf(document):
                try:
                    pdf_bytes = storage.download_bytes(document.blob_storage_path)
                except Exception:  # noqa: BLE001 - only the PDF-based steps below need it
                    pdf_bytes = None
            # Line items read from the OCR layout itself (one-row fee tables,
            # "rate PER unit X qty = total" lines, PAID/DUE markers) and the
            # PDF's producer info — app/services/line_item_parsing.py. Best
            # effort: the model's extraction is kept as is if this fails.
            try:
                extracted_fields = enrich_extracted_fields(
                    extracted_fields,
                    ocr_text=ocr_result.text,
                    ocr_tables=ocr_result.tables,
                    pdf_bytes=pdf_bytes,
                    ocr_pages=ocr_result.pages,
                    signature_regions=_detected_signature_regions(db, document),
                )
            except Exception:  # noqa: BLE001
                pass
            # A file holding several invoices: each one extracted from its own
            # pages (app/services/multi_invoice.py). Best effort.
            try:
                invoices = extract_invoices(get_llm_service(), ocr_result.pages, pdf_bytes)
                if invoices:
                    extracted_fields["invoices"] = invoices
            except Exception:  # noqa: BLE001
                pass
            # Before the document is marked complete, so the case is never
            # scored without it.
            _run_font_consistency(db, document, pdf_bytes, ocr_result.pages)
            from app.services.check_store import sanitize_null_bytes

            document.extracted_fields = sanitize_null_bytes(extracted_fields)
            document.processing_status = DocumentProcessingStatus.complete
            document.processing_error = None

            record_event(
                db,
                "document_processing_completed",
                case_id=document.case_id,
                document_id=document.id,
                event_data={
                    "document_type": analysis.document_type,
                    "document_type_confidence": analysis.document_type_confidence,
                    "fields_located": fields_located,
                },
            )
            db.commit()

            run_document_checks.delay(str(document.id), str(document.company_id))
        except Exception as exc:  # noqa: BLE001 - any failure here must still
            # land as a clean "failed" status + audit row, not an unhandled
            # worker crash the frontend has no way to learn about.
            db.rollback()
            doc_to_fail = db.execute(
                select(Document).where(
                    Document.id == uuid.UUID(document_id), Document.company_id == company_uuid
                )
            ).scalar_one_or_none()
            if doc_to_fail:
                doc_to_fail.processing_status = DocumentProcessingStatus.failed
                doc_to_fail.processing_error = str(exc)[:_ERROR_MESSAGE_MAX_LENGTH]
                record_event(
                    db,
                    "document_processing_failed",
                    case_id=doc_to_fail.case_id,
                    document_id=doc_to_fail.id,
                    event_data={"error": str(exc)[:_ERROR_MESSAGE_MAX_LENGTH]},
                )
                db.commit()

                # A failed document still counts toward "every document in the
                # case has reached a terminal status" (see app/tasks/document_
                # checks.py) — without this, one failed extraction would
                # silently block the case's cross-document check forever.
                _maybe_enqueue_cross_document_check(db, doc_to_fail.company_id, doc_to_fail.case_id)
                request_case_scoring(doc_to_fail.case_id, doc_to_fail.company_id)
    finally:
        db.close()
