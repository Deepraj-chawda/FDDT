"""
Celery task: visual inconsistency review — a vision-capable Azure
OpenAI model's structured per-page read (CLAUDE.md section 3.2's
secondary "Claude vision API" review pass), folded together with the
AI-generated-content question (CLAUDE.md section 3.4) into one check —
see app/services/visual_inconsistency_service.py's module docstring for
the full reasoning (Azure AI Content Safety, the API CLAUDE.md section
2 names for AI-generated-content detection, has no such capability at
all).

Runs on EVERY uploaded PDF, unconditionally — NOT gated on
ela_tampering/copy_move_detection firing first. Enqueued directly from
the upload endpoint (app/api/documents.py) alongside the other
independent forensics tasks, since it only needs the PDF's own rendered
pages, not OCR text or a classified document_type.

PDF-only for now, same gate as every other forensics task in this
codebase (CLAUDE.md's "starting with PDF documents only" scope note).
"""
import uuid

from sqlalchemy import select

from app.db import tenancy
from app.db.session import SessionLocal
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.services.audit_service import record_event
from app.services.check_store import save_check
from app.services.forensics.pdf_render import render_pdf_pages
from app.services.llm_service import get_llm_service
from app.services.storage_service import get_storage_service_for_task
from app.services.visual_inconsistency_service import Finding, run_visual_inconsistency_review
from app.services.risk_scoring_service import request_case_scoring
from app.tasks.celery_app import celery_app
from app.tasks.tenant import open_task_session, resolve_company_for_document

_ERROR_MESSAGE_MAX_LENGTH = 4000


def _is_pdf(document: Document) -> bool:
    if document.content_type == "application/pdf":
        return True
    return document.original_filename.lower().endswith(".pdf")


def _latest_metadata_forensics_result(db, document_id: uuid.UUID) -> dict | None:
    """Best-effort snapshot, not a wait: metadata_forensics
    (app/tasks/metadata_forensics_task.py) and this task are both
    enqueued independently right after upload, so there's no guarantee
    either has finished before the other — this reads whatever's there
    at the moment this task happens to run, same as CLAUDE.md's original
    "cross-check against the metadata_forensics result" ask allows for."""
    check = db.execute(
        select(DocumentCheck)
        .where(
            DocumentCheck.document_id == document_id,
            DocumentCheck.check_type == DocumentCheckType.metadata_forensics,
            DocumentCheck.status == DocumentCheckStatus.completed,
        )
    ).scalar_one_or_none()  # one row per (document, check_type)
    return check.result if check is not None and isinstance(check.result, dict) else None


def _append_metadata_correlation(analysis: dict, metadata_result: dict | None) -> None:
    """Only called when this check's own result is already "flag" — a
    correlation note only means something once there's something on
    this side to correlate. Elevates confidence (severity="high") when
    metadata_forensics agrees, keeps it low/informational when it
    disagrees or hasn't run yet, per CLAUDE.md's original ask for this
    cross-check."""
    if metadata_result is None:
        note = Finding(
            finding="metadata_forensics_correlation",
            severity="info",
            description=(
                "metadata_forensics hasn't completed for this document yet (these checks run "
                "in parallel) — cross-check not available."
            ),
        )
    elif metadata_result.get("result") == "flag":
        note = Finding(
            finding="metadata_forensics_correlation",
            severity="high",
            description=(
                "metadata_forensics also flagged this document — independent techniques "
                "(file-structure/metadata analysis vs. a vision model's visual read) agreeing "
                "is a stronger combined signal than either alone."
            ),
        )
    else:
        note = Finding(
            finding="metadata_forensics_correlation",
            severity="info",
            description=(
                "metadata_forensics found nothing anomalous on this document — a disagreement "
                "between techniques, so treat this check's findings as a lower-confidence "
                "signal on their own, not confirmed tampering."
            ),
        )
    analysis["details"].append(note.to_dict())


@celery_app.task(name="run_visual_inconsistency_review")
def run_visual_inconsistency_review_task(document_id: str, company_id: str | None = None) -> None:
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
                save_check(
                    db,
                    document=document,
                    check_type=DocumentCheckType.visual_inconsistency_review,
                    status=DocumentCheckStatus.completed,
                    result={"result": "not_applicable", "details": []},
                )
            else:
                analysis = run_visual_inconsistency_review(get_llm_service(), pages)
                if analysis["result"] == "flag":
                    _append_metadata_correlation(analysis, _latest_metadata_forensics_result(db, document.id))

                save_check(
                    db,
                    document=document,
                    check_type=DocumentCheckType.visual_inconsistency_review,
                    status=DocumentCheckStatus.completed,
                    result=analysis,
                )
                record_event(
                    db,
                    "visual_inconsistency_review_completed",
                    case_id=document.case_id,
                    document_id=document.id,
                    event_data={
                        "result": analysis["result"],
                        "finding_count": len(analysis["details"]),
                        "page_count": len(pages),
                    },
                )
        except Exception as exc:  # noqa: BLE001 - a check failure must still
            # land as a clean "failed" document_checks row + audit entry,
            # not an unhandled worker crash (e.g. a missing/misconfigured
            # Azure OpenAI deployment raises LLMConfigurationError here).
            error_message = str(exc)[:_ERROR_MESSAGE_MAX_LENGTH]
            save_check(
                db,
                document=document,
                check_type=DocumentCheckType.visual_inconsistency_review,
                status=DocumentCheckStatus.failed,
                error_message=error_message,
            )
            record_event(
                db,
                "visual_inconsistency_review_failed",
                case_id=document.case_id,
                document_id=document.id,
                event_data={"error": error_message},
            )
        db.commit()
        request_case_scoring(document.case_id, document.company_id)
    finally:
        db.close()
