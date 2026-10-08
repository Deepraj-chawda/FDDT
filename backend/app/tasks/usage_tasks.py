"""
Periodic housekeeping tasks (Celery beat — see app/tasks/celery_app.py):

* `reconcile_usage_stats` — nightly. Recomputes every company's usage counters
  from the source tables (app/services/usage_service.py) and corrects any
  drift, writing a platform-level `usage_stats_reconciled` audit row whenever
  something was corrected. The dashboard reads the fast counters; this is the
  correctness backstop.
* `log_queue_metrics` — every minute. One structured log line per queue
  (`queue_metrics {json}`: depth, running, oldest waiting task, average/p95
  wait) so queue health is visible in worker logs, and can be parsed by a log
  shipper, even with no one watching the monitor page. When a queue's oldest
  waiting task exceeds QUEUE_ALERT_OLDEST_WAITING_SECONDS it also logs a
  WARNING `queue_alert {json}` line — the hook for an external alert rule.
"""
import json
import logging

from app.core.config import settings
from app.db.session import system_session
from app.services.audit_service import record_event
from app.services.usage_service import reconcile_usage
from app.tasks.celery_app import celery_app

logger = logging.getLogger("fddt.usage")


def run_reconciliation(trigger: str, actor_user_id=None) -> dict:
    with system_session() as db:
        result = reconcile_usage(db)
        if result.rows_corrected:
            record_event(
                db,
                "usage_stats_reconciled",
                actor_user_id=actor_user_id,
                event_data={
                    "trigger": trigger,
                    "rows_checked": result.rows_checked,
                    "rows_corrected": result.rows_corrected,
                    "drift": result.drift[:100],
                },
            )
        db.commit()
    logger.info(
        "usage reconciliation trigger=%s rows_checked=%d rows_corrected=%d",
        trigger, result.rows_checked, result.rows_corrected,
    )
    return {"rows_checked": result.rows_checked, "rows_corrected": result.rows_corrected, "drift": result.drift}


@celery_app.task(name="reconcile_usage_stats")
def reconcile_usage_stats() -> dict:
    return run_reconciliation("scheduled")


@celery_app.task(name="log_queue_metrics")
def log_queue_metrics() -> None:
    from app.services.queue_monitor import snapshot

    from app.tasks import fairshare

    snap = snapshot(window_seconds=300)
    if not snap.get("available"):
        logger.warning("queue metrics unavailable: %s", snap.get("error"))
        return
    for q in snap["queues"]:
        # Self-heal fair-share counters: an empty queue has nothing outstanding.
        # (This task runs on housekeeping_queue, so it never counts itself.)
        fairshare.reset_if_idle(q["queue"], q["waiting"] == 0 and q["running"] == 0)
        metrics = {
            "queue": q["queue"],
            "waiting": q["waiting"],
            "running": q["running"],
            "oldest_waiting_seconds": q["oldest_waiting_seconds"],
            "avg_wait_seconds": q["avg_wait_seconds"],
            "p95_wait_seconds": q["p95_wait_seconds"],
            "started_in_window": q["started_in_window"],
            "window_seconds": snap["window_seconds"],
        }
        logger.info("queue_metrics %s", json.dumps(metrics, sort_keys=True))
        threshold = settings.queue_alert_oldest_waiting_seconds
        oldest = q["oldest_waiting_seconds"]
        if threshold > 0 and oldest is not None and oldest > threshold:
            logger.warning(
                "queue_alert %s",
                json.dumps({**metrics, "alert": "oldest_waiting_exceeded", "threshold_seconds": threshold}, sort_keys=True),
            )
