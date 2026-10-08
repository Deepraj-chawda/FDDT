"""
Stuck-document recovery (Celery beat, every 5 minutes, on housekeeping_queue).

The queued pipeline tasks live only in the broker Redis. If Redis restarts
without its data (e.g. Azure Managed Redis without high availability during
maintenance), or was unreachable when a document was uploaded
(app/services/document_intake.py then stores the document but can't queue
it), the document would stay `pending` — or `processing`, if its task was
running — forever.

This job finds those documents and queues their pipeline again:

* only while the extraction queue is EMPTY and IDLE (nothing waiting at any
  priority level, nothing running). A pending document's first task is
  `process_document` on that queue, so if it is empty and idle that task is
  not coming; a long backlog therefore never causes duplicate work;
* `pending` for longer than STUCK_DOCUMENT_PENDING_MINUTES (default 10), or
  `processing` with no update for STUCK_DOCUMENT_PROCESSING_MINUTES
  (default 30);
* at most STUCK_DOCUMENT_MAX_REQUEUES times per document (default 3, counted
  from its `document_requeued` audit rows); after that it is marked `failed`
  with an explanation, so a document that keeps crashing its worker can't
  loop forever.

Re-running the pipeline is safe: every step writes idempotently
(app/services/check_store.py — one row per document and check type).
Each re-queue writes a `document_requeued` audit row and the run logs a
WARNING `stuck_documents_requeued {json}` line (hook for an alert).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

from sqlalchemy import and_, func, or_, select

from app.core.config import settings
from app.db.session import SessionLocal, system_session
from app.models.audit_log import AuditLog
from app.models.base import utcnow
from app.models.document import Document, DocumentProcessingStatus
from app.services.audit_service import record_event
from app.services.document_intake import enqueue_document_pipeline
from app.tasks.celery_app import EXTRACTION_QUEUE, celery_app
from app.tasks.tenant import open_task_session

logger = logging.getLogger("fddt.recovery")

REQUEUE_EVENT = "document_requeued"
_ERROR_GAVE_UP = (
    "Processing did not complete after {n} automatic retries (the processing queue lost the "
    "document's tasks each time). Upload the document again, or contact support if it keeps happening."
)


def _extraction_queue_busy() -> bool | None:
    from app.services.queue_monitor import queue_busy

    return queue_busy(EXTRACTION_QUEUE)


def find_stuck_documents(db) -> list[Document]:
    """Documents that look abandoned (see module docstring), oldest first."""
    now = utcnow()
    conditions = []
    if settings.stuck_document_pending_minutes > 0:
        conditions.append(and_(
            Document.processing_status == DocumentProcessingStatus.pending,
            Document.created_at < now - timedelta(minutes=settings.stuck_document_pending_minutes),
        ))
    if settings.stuck_document_processing_minutes > 0:
        conditions.append(and_(
            Document.processing_status == DocumentProcessingStatus.processing,
            Document.updated_at < now - timedelta(minutes=settings.stuck_document_processing_minutes),
        ))
    if not conditions:
        return []
    return list(db.execute(
        select(Document)
        .where(or_(*conditions))
        .order_by(Document.created_at)
        .limit(max(1, settings.stuck_document_batch_size))
    ).scalars())


def _requeue_count(db, document_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.document_id == document_id, AuditLog.event_type == REQUEUE_EVENT
        )
    ).scalar_one()


def _give_up(document_id: uuid.UUID, company_id: uuid.UUID, case_id: uuid.UUID, attempts: int) -> None:
    """Mark the document failed, exactly like a failed extraction, so its case
    can still complete (cross-document check, scoring)."""
    from app.services.risk_scoring_service import request_case_scoring
    from app.tasks.document_checks import _maybe_enqueue_cross_document_check

    db = open_task_session(SessionLocal, company_id)
    try:
        document = db.get(Document, document_id)
        if document is None or document.processing_status not in (
            DocumentProcessingStatus.pending, DocumentProcessingStatus.processing
        ):
            return
        message = _ERROR_GAVE_UP.format(n=attempts)
        document.processing_status = DocumentProcessingStatus.failed
        document.processing_error = message
        record_event(
            db,
            "document_processing_failed",
            case_id=case_id,
            document_id=document_id,
            event_data={"error": message, "reason": "stuck_document_max_requeues"},
        )
        db.commit()
        _maybe_enqueue_cross_document_check(db, company_id, case_id)
        request_case_scoring(case_id, company_id)
    finally:
        db.close()


def requeue_stuck(trigger: str = "scheduled") -> dict:
    busy = _extraction_queue_busy()
    if busy is None:
        logger.warning("stuck-document check skipped: broker (Redis) unreachable")
        return {"skipped": "broker_unreachable"}
    if busy:
        return {"skipped": "extraction_queue_busy"}

    requeued: list[str] = []
    failed: list[str] = []
    with system_session() as db:
        for document in find_stuck_documents(db):
            attempts = _requeue_count(db, document.id)
            if attempts >= settings.stuck_document_max_requeues:
                _give_up(document.id, document.company_id, document.case_id, attempts)
                failed.append(str(document.id))
                continue
            if not enqueue_document_pipeline(document.id, document.company_id):
                break  # broker went away mid-run; the next run tries again
            record_event(
                db,
                REQUEUE_EVENT,
                company_id=document.company_id,
                case_id=document.case_id,
                document_id=document.id,
                event_data={
                    "previous_status": document.processing_status.value,
                    "attempt": attempts + 1,
                    "trigger": trigger,
                },
            )
            db.commit()
            requeued.append(str(document.id))

    result = {"requeued": len(requeued), "marked_failed": len(failed)}
    if requeued or failed:
        logger.warning(
            "stuck_documents_requeued %s",
            json.dumps({**result, "requeued_ids": requeued[:50], "failed_ids": failed[:50]}, sort_keys=True),
        )
    return result


@celery_app.task(name="requeue_stuck_documents")
def requeue_stuck_documents() -> dict:
    return requeue_stuck()
