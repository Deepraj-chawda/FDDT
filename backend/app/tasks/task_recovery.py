"""
Recovery of checks lost by a crashed worker (run by the stuck-document job,
app/tasks/stuck_documents_task.py, every 5 minutes).

Tasks are acknowledged late (app/tasks/celery_app.py): while one runs, its
message sits in the Redis transport's `unacked` hash. If the worker dies
mid-task (OOM kill, node loss, a deploy past its grace period) the message
stays there until the broker's visibility timeout (1 hour) hands it out again,
and the queue monitor counts it as "running" all that time. A document's
other checks finish, but its case waits for the lost one.

Two steps close that gap:

1. restore_orphaned_tasks(): asks the live workers what they are executing or
   have reserved. An `unacked` message older than ORPHANED_TASK_MIN_AGE_SECONDS
   (default 300) whose task no live worker holds is an orphan: it is put back
   on its queue at once (kombu's own restore, the same operation the
   visibility timeout performs). Skipped for a queue none of whose workers
   replied (nothing can be concluded), and entirely if no worker replied.
   The visibility timeout stays the last resort, so a live task is never
   handed out twice because of a slow reply.

2. requeue_missing_checks(): a document whose extraction finished more than
   STUCK_CHECK_MINUTES (default 15) ago but which still lacks one of its
   checks, while that check's queue is empty and idle, gets just that check's
   task queued again (its message was lost outright, e.g. Redis restarted
   without its data). At most STUCK_DOCUMENT_MAX_REQUEUES times per document;
   then the missing checks are recorded as `failed` with an explanation, so
   the case can still be scored and decided instead of waiting forever.

Re-running a check is safe: every check writes idempotently
(app/services/check_store.py — one row per document and check type).
Both steps log a WARNING line when they act (hook for an alert).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import timedelta

from sqlalchemy import func, select

from app.core.config import settings
from app.db.session import SessionLocal, system_session
from app.models.audit_log import AuditLog
from app.models.base import utcnow
from app.models.document import Document, DocumentProcessingStatus
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.services.audit_service import record_event
from app.services.check_store import save_check
from app.tasks.celery_app import TASK_QUEUES, celery_app
from app.tasks.tenant import open_task_session

logger = logging.getLogger("fddt.recovery")

CHECKS_REQUEUED_EVENT = "document_checks_requeued"
_INSPECT_TIMEOUT_SECONDS = 3.0
_ERROR_GAVE_UP = (
    "This check did not complete after {n} automatic retries (its task was lost each time). "
    "Upload the document again to re-run it."
)

# Which task writes each check. One task can write several checks.
_TASK_FOR_CHECK: dict[DocumentCheckType, str] = {
    DocumentCheckType.field_validation: "run_document_checks",
    DocumentCheckType.issuer_verification: "run_document_checks",
    DocumentCheckType.metadata_forensics: "run_metadata_forensics",
    DocumentCheckType.error_level_analysis: "run_tampering_checks",
    DocumentCheckType.copy_move_detection: "run_tampering_checks",
    DocumentCheckType.duplicate_detection: "run_duplicate_check",
    DocumentCheckType.visual_inconsistency_review: "run_visual_inconsistency_review",
    DocumentCheckType.signature_stamp_detection: "run_signature_detection",
}
_EXTRACTION_CHECKS = (DocumentCheckType.field_validation, DocumentCheckType.issuer_verification)
_PDF_CHECKS = (
    DocumentCheckType.metadata_forensics,
    DocumentCheckType.error_level_analysis,
    DocumentCheckType.copy_move_detection,
    DocumentCheckType.duplicate_detection,
    DocumentCheckType.visual_inconsistency_review,
    DocumentCheckType.signature_stamp_detection,
)
_TERMINAL = (DocumentCheckStatus.completed, DocumentCheckStatus.failed)


# ---------------------------------------------------------------------------
# 1. Orphaned (unacknowledged, not executing) tasks
# ---------------------------------------------------------------------------

def _live_workers() -> tuple[set[str], set[str]] | None:
    """(task ids live workers are executing or have reserved, queues those
    workers consume), or None if no worker replied."""
    inspect = celery_app.control.inspect(timeout=_INSPECT_TIMEOUT_SECONDS)
    active = inspect.active()
    if not active:
        return None
    reserved = inspect.reserved() or {}
    queues = inspect.active_queues() or {}
    ids = {t["id"] for tasks in (*active.values(), *reserved.values()) for t in tasks or [] if t.get("id")}
    names = {q["name"] for qs in queues.values() for q in qs or [] if q.get("name")}
    return ids, names


def _message_task_id(message: dict) -> str | None:
    return (message.get("headers") or {}).get("id") or (message.get("properties") or {}).get("correlation_id")


def restore_orphaned_tasks() -> dict:
    min_age = settings.orphaned_task_min_age_seconds
    if min_age <= 0:
        return {"skipped": "disabled"}
    try:
        live = _live_workers()
    except Exception:  # noqa: BLE001 - broker unreachable: try again next run
        logger.warning("orphaned-task check skipped: worker inspection failed", exc_info=True)
        return {"skipped": "inspect_failed"}
    if live is None:
        return {"skipped": "no_worker_replied"}
    live_ids, live_queues = live

    restored: list[dict] = []
    now = time.time()
    with celery_app.connection_for_write() as conn:
        channel = conn.default_channel
        qos, client = channel.qos, channel.client
        for tag, raw in client.hgetall(qos.unacked_key).items():
            try:
                message, _exchange, routing_key = json.loads(raw)
            except (ValueError, TypeError):
                continue
            task_id = _message_task_id(message)
            if not task_id or task_id in live_ids or routing_key not in live_queues:
                continue
            delivered_at = client.zscore(qos.unacked_index_key, tag)
            if delivered_at is None or now - delivered_at < min_age:
                continue
            qos.restore_by_tag(tag)
            restored.append({
                "task_id": task_id,
                "task": (message.get("headers") or {}).get("task"),
                "queue": routing_key,
                "age_seconds": int(now - delivered_at),
            })
    if restored:
        logger.warning("orphaned_tasks_restored %s", json.dumps({"count": len(restored), "tasks": restored[:50]}))
    return {"restored": len(restored)}


# ---------------------------------------------------------------------------
# 2. Checks still missing on documents whose extraction finished
# ---------------------------------------------------------------------------

def _is_pdf(document: Document) -> bool:
    from app.services.risk_scoring_service import is_pdf_document

    return is_pdf_document(document)


def _expected_checks(document: Document) -> tuple[DocumentCheckType, ...]:
    expected: tuple[DocumentCheckType, ...] = ()
    if document.processing_status == DocumentProcessingStatus.complete:
        expected += _EXTRACTION_CHECKS
    if _is_pdf(document):
        expected += _PDF_CHECKS
    return expected


def _missing_by_document(db) -> list[tuple[Document, list[DocumentCheckType]]]:
    cutoff = utcnow() - timedelta(minutes=settings.stuck_check_minutes)
    documents = list(db.execute(
        select(Document)
        .where(
            Document.processing_status.in_((DocumentProcessingStatus.complete, DocumentProcessingStatus.failed)),
            Document.updated_at < cutoff,
            Document.updated_at > cutoff - timedelta(days=2),  # don't resurrect old history
        )
        .order_by(Document.updated_at)
        .limit(max(1, settings.stuck_document_batch_size))
    ).scalars())
    if not documents:
        return []
    done: dict[uuid.UUID, set[DocumentCheckType]] = {}
    for document_id, check_type in db.execute(
        select(DocumentCheck.document_id, DocumentCheck.check_type).where(
            DocumentCheck.document_id.in_([d.id for d in documents]),
            DocumentCheck.status.in_(_TERMINAL),
        )
    ).all():
        done.setdefault(document_id, set()).add(check_type)
    out = []
    for document in documents:
        missing = [c for c in _expected_checks(document) if c not in done.get(document.id, set())]
        if missing:
            out.append((document, missing))
    return out


def _requeue_count(db, document_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count()).select_from(AuditLog).where(
            AuditLog.document_id == document_id, AuditLog.event_type == CHECKS_REQUEUED_EVENT
        )
    ).scalar_one()


def _give_up(document: Document, missing: list[DocumentCheckType], attempts: int) -> None:
    """Record the lost checks as failed so the case can complete."""
    from app.services.risk_scoring_service import request_case_scoring
    from app.tasks.document_checks import _maybe_enqueue_cross_document_check

    message = _ERROR_GAVE_UP.format(n=attempts)
    db = open_task_session(SessionLocal, document.company_id)
    try:
        doc = db.get(Document, document.id)
        if doc is None:
            return
        for check_type in missing:
            save_check(db, document=doc, check_type=check_type, status=DocumentCheckStatus.failed, error_message=message)
        record_event(
            db, "document_checks_failed", case_id=doc.case_id, document_id=doc.id,
            event_data={"checks": [c.value for c in missing], "error": message, "reason": "stuck_check_max_requeues"},
        )
        db.commit()
        _maybe_enqueue_cross_document_check(db, doc.company_id, doc.case_id)
        request_case_scoring(doc.case_id, doc.company_id)
    finally:
        db.close()


def requeue_missing_checks() -> dict:
    if settings.stuck_check_minutes <= 0:
        return {"skipped": "disabled"}
    from app.services.queue_monitor import queue_busy

    busy: dict[str, bool | None] = {}

    def queue_free(queue: str) -> bool:
        if queue not in busy:
            busy[queue] = queue_busy(queue)
        return busy[queue] is False  # None = broker unreachable: don't act

    requeued: list[dict] = []
    failed: list[str] = []
    with system_session() as db:
        for document, missing in _missing_by_document(db):
            tasks = sorted({_TASK_FOR_CHECK[c] for c in missing})
            if not all(queue_free(TASK_QUEUES[t]) for t in tasks):
                continue  # still working through a backlog: the task may yet run
            attempts = _requeue_count(db, document.id)
            if attempts >= settings.stuck_document_max_requeues:
                _give_up(document, missing, attempts)
                failed.append(str(document.id))
                continue
            args = (str(document.id), str(document.company_id))
            for name in tasks:
                celery_app.tasks[name].delay(*args)
            record_event(
                db, CHECKS_REQUEUED_EVENT, company_id=document.company_id, case_id=document.case_id,
                document_id=document.id,
                event_data={"checks": [c.value for c in missing], "tasks": tasks, "attempt": attempts + 1},
            )
            db.commit()
            requeued.append({"document_id": str(document.id), "checks": [c.value for c in missing]})
    if requeued or failed:
        logger.warning(
            "stuck_checks_requeued %s",
            json.dumps({"requeued": requeued[:50], "marked_failed": failed[:50]}, sort_keys=True),
        )
    return {"requeued": len(requeued), "marked_failed": len(failed)}
