"""
Celery task: automatic signature/stamp detection on every uploaded PDF
(SPECIFICATION.md §2.3). Detection only — no comparison happens here except to
re-trigger comparison for references a reviewer already created in this
case (see the end of `run_signature_detection`).

PDF-only for now, same gate as every other forensics task here.
"""
import uuid

from sqlalchemy import select

from app.db import tenancy
from app.db.session import SessionLocal
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.models.signature_reference import SignatureReference
from app.services.audit_service import record_event
from app.services.check_store import save_check
from app.services.forensics.pdf_render import render_pdf_pages
from app.services.llm_service import get_llm_service
from app.services.risk_scoring_service import request_case_scoring
from app.services.signature_detection_service import detect_signatures
from app.services.storage_service import get_storage_service_for_task
from app.tasks.celery_app import celery_app
from app.tasks.document_checks import revalidate_fields
from app.tasks.tenant import open_task_session, resolve_company_for_document
from app.tasks.signature_comparison_task import run_signature_comparison
from app.tasks.tampering_checks_task import refilter_ghost_content
from app.tasks.visual_inconsistency_task import refilter_visual_review

_ERROR_MESSAGE_MAX_LENGTH = 4000


def _is_pdf(document: Document) -> bool:
    if document.content_type == "application/pdf":
        return True
    return document.original_filename.lower().endswith(".pdf")


@celery_app.task(name="run_signature_detection")
def run_signature_detection(document_id: str, company_id: str | None = None) -> None:
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

            if not pages:
                result = {"result": "not_applicable", "details": {"bounding_box": None}}
            else:
                result = detect_signatures(get_llm_service(), pages, pdf_bytes)

            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.signature_stamp_detection,
                status=DocumentCheckStatus.completed,
                result=result,
            )
            # The visual review's signature/stamp filter needs these regions;
            # re-apply it if that review finished first.
            if pages:
                try:
                    refilter_visual_review(db, document, pages)
                except Exception:  # noqa: BLE001 - never fail detection over it
                    pass
            # So does field validation's stamp-vs-issuer sub-check.
            try:
                revalidate_fields(db, document)
            except Exception:  # noqa: BLE001 - never fail detection over it
                pass
            # And the ghost-content check: traces inside a signature or
            # stamp are its ink, not deleted text.
            try:
                refilter_ghost_content(db, document)
            except Exception:  # noqa: BLE001 - never fail detection over it
                pass
            record_event(
                db,
                "signature_stamp_detection_completed",
                case_id=document.case_id,
                document_id=document.id,
                event_data={
                    "result": result["result"],
                    "regions_detected": len(result["details"].get("detected", [])),
                },
            )
        except Exception as exc:  # noqa: BLE001 - land as a clean "failed" check + audit entry
            error_message = str(exc)[:_ERROR_MESSAGE_MAX_LENGTH]
            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.signature_stamp_detection,
                status=DocumentCheckStatus.failed,
                error_message=error_message,
            )
            record_event(
                db,
                "signature_stamp_detection_failed",
                case_id=document.case_id,
                document_id=document.id,
                event_data={"error": error_message},
            )
        db.commit()
        request_case_scoring(document.case_id, document.company_id)

        # A reviewer may have created a reference before this document's
        # detection finished (they run in parallel with the rest of the
        # upload pipeline). Comparison skips documents whose detection isn't
        # done yet, so re-run it for this case's other-document references
        # now — it is idempotent, so already-compared pairs are left alone.
        reference_ids = db.execute(
            select(SignatureReference.id).where(
                SignatureReference.company_id == document.company_id,
                SignatureReference.source_case_id == document.case_id,
                SignatureReference.source_document_id != document.id,
            )
        ).scalars().all()
        for reference_id in reference_ids:
            run_signature_comparison.delay(str(reference_id), str(document.company_id))
    finally:
        db.close()
