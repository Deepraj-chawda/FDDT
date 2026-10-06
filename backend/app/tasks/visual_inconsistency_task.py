"""
Celery task: visual inconsistency review — a vision-capable Azure
OpenAI model's structured per-page read (SPECIFICATION.md section 3.2's
secondary vision-model review pass), folded together with the
AI-generated-content question (SPECIFICATION.md section 3.4) into one check —
see app/services/visual_inconsistency_service.py's module docstring for
the full reasoning (Azure AI Content Safety, the API SPECIFICATION.md section
2 names for AI-generated-content detection, has no such capability at
all).

Runs on EVERY uploaded PDF, unconditionally — NOT gated on
ela_tampering/copy_move_detection firing first. Enqueued directly from
the upload endpoint (app/api/documents.py) alongside the other
independent forensics tasks, since it only needs the PDF's own rendered
pages, not OCR text or a classified document_type.

PDF-only for now, same gate as every other forensics task in this
codebase (SPECIFICATION.md's "starting with PDF documents only" scope note).
"""
import uuid

from sqlalchemy import select, text

from app.db import tenancy
from app.db.session import SessionLocal
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.services.audit_service import record_event
from app.services.check_store import save_check
from app.services.forensics.pdf_render import render_pdf_pages
from app.services.llm_service import get_llm_service
from app.services.storage_service import get_storage_service_for_task
from app.services.visual_inconsistency_service import (
    Finding,
    apply_region_filters,
    run_visual_inconsistency_review,
)
from app.services.risk_scoring_service import request_case_scoring
from app.tasks.celery_app import celery_app
from app.tasks.tenant import open_task_session, resolve_company_for_document

_ERROR_MESSAGE_MAX_LENGTH = 4000


def _is_pdf(document: Document) -> bool:
    if document.content_type == "application/pdf":
        return True
    return document.original_filename.lower().endswith(".pdf")


def lock_visual_filters(db, document_id: uuid.UUID) -> None:
    """Serialise, per document, the two writers of the visual review's
    region filters: this task (filters with whatever signature detection
    exists) and signature detection (re-filters the stored review once it has
    regions). Each takes this transaction-scoped lock before reading the
    other's row, so whichever runs second sees the first's committed result."""
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"visual-filters:{document_id}"})


def completed_check_result(db, document_id: uuid.UUID, check_type: DocumentCheckType) -> dict | None:
    check = db.execute(
        select(DocumentCheck).where(
            DocumentCheck.document_id == document_id,
            DocumentCheck.check_type == check_type,
            DocumentCheck.status == DocumentCheckStatus.completed,
        )
    ).scalar_one_or_none()
    return check.result if check is not None and isinstance(check.result, dict) else None


def refilter_visual_review(db, document: Document, pages) -> bool:
    """Re-apply the region filters to the stored visual review with the
    current signature/stamp regions (called by signature detection once it
    has them). Returns whether the stored result changed. The caller
    commits."""
    lock_visual_filters(db, document.id)
    visual = completed_check_result(db, document.id, DocumentCheckType.visual_inconsistency_review)
    if visual is None or visual.get("result") not in ("pass", "flag"):
        return False
    import copy

    before = copy.deepcopy(visual)
    signature = completed_check_result(db, document.id, DocumentCheckType.signature_stamp_detection)
    after = apply_region_filters(visual, signature, pages)
    if after == before:
        return False
    save_check(
        db,
        document=document,
        check_type=DocumentCheckType.visual_inconsistency_review,
        status=DocumentCheckStatus.completed,
        result=after,
    )
    return True


def _latest_metadata_forensics_result(db, document_id: uuid.UUID) -> dict | None:
    """Best-effort snapshot, not a wait: metadata_forensics
    (app/tasks/metadata_forensics_task.py) and this task are both
    enqueued independently right after upload, so there's no guarantee
    either has finished before the other — this reads whatever's there
    at the moment this task happens to run, same as SPECIFICATION.md's original
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
    disagrees or hasn't run yet, per SPECIFICATION.md's original ask for this
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
                # Signature/stamp and scan-skew explanations (see
                # apply_region_filters). Signature detection runs in
                # parallel; if it finishes later it re-applies these.
                lock_visual_filters(db, document.id)
                analysis = apply_region_filters(
                    analysis,
                    completed_check_result(db, document.id, DocumentCheckType.signature_stamp_detection),
                    pages,
                )
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
