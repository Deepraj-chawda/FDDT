"""
Fair-share scheduling across companies on the shared processing queues.

Strict FIFO let one company's large upload block everyone who submitted after
it. Instead, every pipeline task is published with a priority derived from how
much work ITS company already has outstanding (queued + running) on the same
queue:

    priority = min(9, outstanding_before_this_task // FAIR_SHARE_BUCKET_SIZE)

Celery's Redis transport keeps one list per priority level and workers always
take from the lowest number first (0 = highest). So a company with little
outstanding work lands in the front lists, while the tail of a big batch lands
in the back ones — a single document from Company B is served after at most a
bucket's worth of Company A's tasks, not after all of them.

This is equal share, not a ranking: no company has a fixed tier, and the
priority is recomputed for every task from live counters. As a company's
backlog drains its new tasks move forward again. Within one priority level
order stays FIFO.

Counters: one Redis hash per queue (`fddt:fairshare:<queue>`, field =
company_id), incremented when a task is published and decremented when it
finishes (task_postrun, success or failure). A task that is redelivered after
a worker crash is not re-published, so it is counted once and decremented
once. Revoked or lost messages could leave a counter high; the per-minute
queue-metrics job resets a queue's counters whenever that queue is completely
empty (app/tasks/usage_tasks.py), so drift is self-healing.

If Redis can't be reached the task is published without a priority (plain
FIFO) — scheduling degrades, processing never stops.
"""
from __future__ import annotations

import logging
from functools import lru_cache

from celery import Task

logger = logging.getLogger("fddt.fairshare")

COMPANY_HEADER = "fddt_company_id"
QUEUE_HEADER = "fddt_fair_queue"
MAX_PRIORITY = 9
_KEY = "fddt:fairshare:{queue}"


@lru_cache
def _redis():
    import redis

    from app.core.config import settings

    return redis.Redis.from_url(settings.celery_broker_url, socket_connect_timeout=0.5, socket_timeout=2)


def bucket_size() -> int:
    from app.core.config import settings

    return max(1, settings.fair_share_bucket_size)


def priority_for(outstanding_before: int) -> int:
    return min(MAX_PRIORITY, outstanding_before // bucket_size())


def outstanding(queue: str) -> dict[str, int]:
    """Current outstanding (queued + running) task count per company."""
    try:
        raw = _redis().hgetall(_KEY.format(queue=queue))
    except Exception:  # noqa: BLE001
        return {}
    return {k.decode(): int(v) for k, v in raw.items() if int(v) > 0}


def task_finished(queue: str | None, company_id: str | None) -> None:
    if not queue or not company_id:
        return
    try:
        client = _redis()
        if client.hincrby(_KEY.format(queue=queue), company_id, -1) <= 0:
            client.hdel(_KEY.format(queue=queue), company_id)
    except Exception:  # noqa: BLE001
        logger.debug("fair-share: could not decrement", exc_info=True)


def reset_if_idle(queue: str, idle: bool) -> None:
    """Self-healing: a completely empty queue has nothing outstanding."""
    if idle:
        try:
            _redis().delete(_KEY.format(queue=queue))
        except Exception:  # noqa: BLE001
            pass


class FairShareTask(Task):
    """Base class for every task (`task_cls` on the Celery app). Pipeline
    tasks take (resource_id, company_id, ...): when a company id is present,
    the publish gets a fair-share priority and the headers the worker needs to
    decrement the counter afterwards."""

    def apply_async(self, args=None, kwargs=None, task_id=None, producer=None, link=None, link_error=None,
                    shadow=None, **options):
        company_id = None
        if args and len(args) > 1 and args[1]:
            company_id = str(args[1])
        elif kwargs and kwargs.get("company_id"):
            company_id = str(kwargs["company_id"])
        if company_id and "priority" not in options:
            from app.tasks.celery_app import TASK_QUEUES, FORENSICS_QUEUE

            queue = options.get("queue") or TASK_QUEUES.get(self.name, FORENSICS_QUEUE)
            try:
                count = _redis().hincrby(_KEY.format(queue=queue), company_id, 1)
                options["priority"] = priority_for(count - 1)
                headers = dict(options.get("headers") or {})
                headers[COMPANY_HEADER] = company_id
                headers[QUEUE_HEADER] = queue
                options["headers"] = headers
            except Exception:  # noqa: BLE001
                logger.warning("fair-share: Redis unavailable, publishing without priority", exc_info=True)
        return super().apply_async(args, kwargs, task_id=task_id, producer=producer, link=link,
                                   link_error=link_error, shadow=shadow, **options)
