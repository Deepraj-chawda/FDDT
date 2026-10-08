"""
Celery application instance — wired to Redis (broker + result backend) via
env-var-driven settings.

QUEUES — one per external dependency, each served by its own independently
scalable worker pool (start-worker.sh <queue>):

  extraction_queue  Azure Document Intelligence (OCR/layout) — process_document.
                    Calls are globally capped (default 10/s, under Azure's 15
                    TPS) by app/services/rate_limiter.py; size this pool to
                    just saturate that cap.
  vision_queue      Azure OpenAI — visual-inconsistency / AI-generation review,
                    signature detection + comparison, issuer verification's
                    semantic fallback. Capped at the deployment's RPM
                    (AZURE_OPENAI_MAX_REQUESTS_PER_MINUTE); size to saturate it.
  forensics_queue   Local CPU only — metadata forensics, ELA + copy-move,
                    duplicate hashing — plus the light DB steps (cross-document
                    check, risk scoring). NO rate limit: scale freely with
                    cores, down to zero replicas when there is no work.
  housekeeping_queue  Periodic platform jobs (queue metrics, usage
                    reconciliation, stuck-document recovery). Served by the
                    single beat container (start-beat.sh runs beat + a
                    one-slot worker for this queue), so these every-minute
                    jobs never wake the forensics workers.

FAIRNESS — every document's every step is its own task (enqueued per
document by app/api/documents.py; no task ever processes a whole batch or
case), all companies share the same queues, and dispatch is FAIR-SHARE PER
COMPANY (app/tasks/fairshare.py): each task's priority comes from how much
work its company already has outstanding on that queue, so one company's big
batch can't starve another company's single document. No company has a fixed
priority tier. With `worker_prefetch_multiplier=1` + `task_acks_late` a worker
holds only the task it is running.

`include` lists every task module because the worker process only imports
this module (never app.main).

Wait-time instrumentation (before_task_publish / task_prerun signals) feeds
the platform admin's queue monitor (app/services/queue_monitor.py).
"""
import time

from celery import Celery
from celery.schedules import crontab
from celery.signals import before_task_publish, task_postrun, task_prerun

from app.core.config import settings

EXTRACTION_QUEUE = "extraction_queue"
VISION_QUEUE = "vision_queue"
FORENSICS_QUEUE = "forensics_queue"
QUEUES = (EXTRACTION_QUEUE, VISION_QUEUE, FORENSICS_QUEUE)
# Not a processing queue (not in QUEUES / the monitor): platform jobs only.
HOUSEKEEPING_QUEUE = "housekeeping_queue"

# Task name -> queue. Anything unlisted falls to forensics_queue (the default),
# which is always served (its autoscaler starts a worker as soon as a task
# waits), so nothing is ever stranded on an unserved queue.
TASK_QUEUES: dict[str, str] = {
    "process_document": EXTRACTION_QUEUE,
    "run_document_checks": VISION_QUEUE,
    "run_visual_inconsistency_review": VISION_QUEUE,
    "run_signature_detection": VISION_QUEUE,
    "run_signature_comparison": VISION_QUEUE,
    "run_metadata_forensics": FORENSICS_QUEUE,
    "run_tampering_checks": FORENSICS_QUEUE,
    "run_duplicate_check": FORENSICS_QUEUE,
    "run_cross_document_checks": FORENSICS_QUEUE,
    "score_case": FORENSICS_QUEUE,
    # Bulk zip ingestion: unzip + PDF parsing, local CPU only. Each document
    # it creates is then queued like any single upload.
    "ingest_bulk_upload": FORENSICS_QUEUE,
    "reconcile_usage_stats": HOUSEKEEPING_QUEUE,
    "log_queue_metrics": HOUSEKEEPING_QUEUE,
    "requeue_stuck_documents": HOUSEKEEPING_QUEUE,
}

celery_app = Celery(
    "document_authenticator",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    # Fair-share priority on publish (see app/tasks/fairshare.py).
    task_cls="app.tasks.fairshare:FairShareTask",
    include=[
        "app.tasks.document_processing",
        "app.tasks.document_checks",
        "app.tasks.metadata_forensics_task",
        "app.tasks.tampering_checks_task",
        "app.tasks.duplicate_check_task",
        "app.tasks.visual_inconsistency_task",
        "app.tasks.signature_detection_task",
        "app.tasks.signature_comparison_task",
        "app.tasks.risk_scoring_task",
        "app.tasks.usage_tasks",
        "app.tasks.stuck_documents_task",
        "app.tasks.bulk_upload_task",
    ],
)

celery_app.conf.update(
    task_routes={name: {"queue": queue} for name, queue in TASK_QUEUES.items()},
    task_default_queue=FORENSICS_QUEUE,
    # Fair dispatch: a worker reserves one task at a time and acknowledges it
    # only when done, so queued work is never parked behind a long task on a
    # busy worker while another worker sits idle.
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Results are never read (every task records its outcome in the DB).
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
    # Ten priority levels per queue on the Redis transport (one list each:
    # "<queue>", "<queue>:1" … "<queue>:9"); workers drain 0 first.
    broker_transport_options={
        "priority_steps": list(range(10)),
        "sep": ":",
        "queue_order_strategy": "priority",
    },
    timezone="UTC",
    beat_schedule={
        # Correctness backstop for the billing counters (see
        # app/services/usage_service.py).
        "reconcile-usage-stats-nightly": {
            "task": "reconcile_usage_stats",
            "schedule": crontab(hour=settings.usage_reconciliation_hour_utc, minute=0),
        },
        # Queue depth / wait time to the worker log every minute, so the data
        # exists even when nobody has the monitor page open.
        "log-queue-metrics": {"task": "log_queue_metrics", "schedule": 60.0},
        # Re-queue documents whose tasks were lost (Redis restart / outage).
        "requeue-stuck-documents": {"task": "requeue_stuck_documents", "schedule": 300.0},
    },
)


# ---------------------------------------------------------------------------
# Wait-time instrumentation
# ---------------------------------------------------------------------------

ENQUEUED_AT_HEADER = "fddt_enqueued_at"


@before_task_publish.connect
def _stamp_enqueue_time(headers=None, **_kwargs):
    if headers is not None:
        headers.setdefault(ENQUEUED_AT_HEADER, time.time())


@task_prerun.connect
def _record_wait(task_id=None, task=None, **_kwargs):
    from app.services import queue_monitor

    request = getattr(task, "request", None)
    if request is None:
        return
    enqueued_at = getattr(request, ENQUEUED_AT_HEADER, None)
    if enqueued_at is None:
        enqueued_at = (getattr(request, "headers", None) or {}).get(ENQUEUED_AT_HEADER)
    queue = (getattr(request, "delivery_info", None) or {}).get("routing_key") or TASK_QUEUES.get(
        task.name, FORENSICS_QUEUE
    )
    request.fddt_started_at = time.time()
    request.fddt_queue = queue
    queue_monitor.record_task_started(queue, task.name, enqueued_at)


@task_postrun.connect
def _record_runtime(task_id=None, task=None, **_kwargs):
    from app.services import queue_monitor

    request = getattr(task, "request", None)
    started = getattr(request, "fddt_started_at", None)
    if started is None:
        return
    queue_monitor.record_task_finished(getattr(request, "fddt_queue", FORENSICS_QUEUE), time.time() - started)


@task_postrun.connect
def _release_fair_share(task=None, **_kwargs):
    """The task is done (success or failure): its company has one less
    outstanding task on this queue."""
    from app.tasks import fairshare

    request = getattr(task, "request", None)
    if request is None:
        return
    headers = getattr(request, "headers", None) or {}
    company = getattr(request, fairshare.COMPANY_HEADER, None) or headers.get(fairshare.COMPANY_HEADER)
    queue = getattr(request, fairshare.QUEUE_HEADER, None) or headers.get(fairshare.QUEUE_HEADER)
    fairshare.task_finished(queue, company)
