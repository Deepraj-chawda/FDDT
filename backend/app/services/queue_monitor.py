"""
Queue depth and wait-time visibility — how the platform admin confirms that
nobody is waiting unfairly and whether rate limits / worker counts need
tuning.

Data sources (all in the broker Redis, so it is shared by every worker and the
API):

* Depth — each Celery queue is a Redis list; LLEN is the number of tasks
  waiting. The oldest waiting task's age comes from its enqueue-time header
  (stamped by app/tasks/celery_app.py on publish).
* Wait time — when a worker starts a task it records (now - enqueued_at) in a
  capped per-queue list; averages/p95 are computed over the recent window.
* Running — tasks reserved by a worker but not yet acknowledged
  (`task_acks_late`), i.e. currently executing.
* Rate limiter counters (app/services/rate_limiter.py) for the two
  Azure-bound queues.

Recording never raises: monitoring must not break task execution.
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from functools import lru_cache
from typing import Any

from app.core.config import settings

logger = logging.getLogger("fddt.queue_monitor")

_WAITS_KEY = "fddt:queue:{queue}:waits"  # list of "started_at:wait_seconds:task_name"
_RUNTIMES_KEY = "fddt:queue:{queue}:runtimes"  # list of "finished_at:seconds"
_SAMPLES = 1000


@lru_cache
def _redis():
    import redis

    return redis.Redis.from_url(
        settings.celery_broker_url, socket_connect_timeout=0.5, socket_timeout=2
    )


def record_task_started(queue: str, task_name: str, enqueued_at: Any) -> None:
    try:
        now = time.time()
        wait = max(0.0, now - float(enqueued_at)) if enqueued_at is not None else None
        if wait is None:
            return
        pipe = _redis().pipeline()
        pipe.lpush(_WAITS_KEY.format(queue=queue), f"{now:.3f}:{wait:.3f}:{task_name}")
        pipe.ltrim(_WAITS_KEY.format(queue=queue), 0, _SAMPLES - 1)
        pipe.execute()
    except Exception:  # noqa: BLE001
        logger.debug("queue monitor: could not record wait", exc_info=True)


def record_task_finished(queue: str, seconds: float) -> None:
    try:
        pipe = _redis().pipeline()
        pipe.lpush(_RUNTIMES_KEY.format(queue=queue), f"{time.time():.3f}:{seconds:.3f}")
        pipe.ltrim(_RUNTIMES_KEY.format(queue=queue), 0, _SAMPLES - 1)
        pipe.execute()
    except Exception:  # noqa: BLE001
        logger.debug("queue monitor: could not record runtime", exc_info=True)


def priority_lists(queue: str) -> list[str]:
    """The Redis lists behind one Celery queue: one per fair-share priority
    level ("<queue>", "<queue>:1" … "<queue>:9")."""
    return [queue] + [f"{queue}:{p}" for p in range(1, 10)]


def queue_depth(client, queue: str) -> int:
    return sum(int(client.llen(name)) for name in priority_lists(queue))


def queue_busy(queue: str) -> bool | None:
    """True if `queue` has a task waiting (any priority level) or running;
    None if the broker can't be reached (the caller can't tell either way)."""
    try:
        client = _redis()
        client.ping()
        return queue_depth(client, queue) > 0 or _running_counts(client).get(queue, 0) > 0
    except Exception:  # noqa: BLE001
        return None


def _oldest_waiting_age(client, queue: str, now: float) -> float | None:
    # Celery's Redis transport LPUSHes and BRPOPs: each list's oldest message
    # is last. The oldest overall is the oldest across the priority lists.
    oldest = None
    for name in priority_lists(queue):
        raw = client.lindex(name, -1)
        if not raw:
            continue
        try:
            message = json.loads(raw)
            enqueued_at = (message.get("headers") or {}).get("fddt_enqueued_at")
        except (ValueError, TypeError):
            continue
        if enqueued_at:
            age = now - float(enqueued_at)
            oldest = age if oldest is None else max(oldest, age)
    return round(oldest, 1) if oldest is not None else None


def _running_counts(client) -> dict[str, int]:
    """Tasks reserved (executing) per queue, from the transport's unacked
    hash."""
    counts: dict[str, int] = {}
    try:
        for raw in client.hvals("unacked"):
            try:
                payload = json.loads(raw)
                delivery = payload[0] if isinstance(payload, list) else payload
                queue = (delivery.get("properties") or {}).get("delivery_info", {}).get("routing_key")
                if queue:
                    counts[queue] = counts.get(queue, 0) + 1
            except (ValueError, TypeError, AttributeError, IndexError):
                continue
    except Exception:  # noqa: BLE001
        pass
    return counts


def _recent(client, key: str, since: float) -> list[tuple[float, float, str]]:
    out = []
    for raw in client.lrange(key, 0, _SAMPLES - 1):
        parts = raw.decode().split(":", 2)
        try:
            stamp, value = float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            continue
        if stamp >= since:
            out.append((stamp, value, parts[2] if len(parts) > 2 else ""))
    return out


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    return round(statistics.quantiles(values, n=20, method="inclusive")[18], 1)


def snapshot(window_seconds: int = 900) -> dict[str, Any]:
    """Current state of every processing queue (for the monitor endpoint and
    the per-minute log line)."""
    from app.services import rate_limiter
    from app.tasks.celery_app import EXTRACTION_QUEUE, FORENSICS_QUEUE, QUEUES, VISION_QUEUE

    limits = {
        EXTRACTION_QUEUE: {
            "service": "Azure Document Intelligence",
            "limiter": rate_limiter.AZURE_DOCUMENT_INTELLIGENCE,
            "max_calls": settings.azure_document_intelligence_max_calls_per_second,
            "period_seconds": 1.0,
            "description": f"{settings.azure_document_intelligence_max_calls_per_second:g} calls/second (global)",
            "configured_workers": settings.extraction_worker_concurrency,
        },
        VISION_QUEUE: {
            "service": "Azure OpenAI",
            "limiter": rate_limiter.AZURE_OPENAI,
            "max_calls": settings.azure_openai_max_requests_per_minute,
            "period_seconds": 60.0,
            "description": (
                f"{settings.azure_openai_max_requests_per_minute:g} requests/minute"
                + (
                    f" and {settings.azure_openai_max_tokens_per_minute:g} tokens/minute"
                    if settings.azure_openai_max_tokens_per_minute > 0
                    else ""
                )
                + " (global)"
            ),
            "configured_workers": settings.vision_worker_concurrency,
            "token_limiter": rate_limiter.AZURE_OPENAI_TOKENS
            if settings.azure_openai_max_tokens_per_minute > 0
            else None,
        },
        FORENSICS_QUEUE: {
            "service": "Local CPU",
            "limiter": None,
            "description": "No rate limit",
            "configured_workers": settings.forensics_worker_concurrency or "CPU cores",
        },
    }
    now = time.time()
    try:
        client = _redis()
        client.ping()
    except Exception:  # noqa: BLE001
        return {"available": False, "error": "Broker (Redis) is unreachable.", "queues": []}

    running = _running_counts(client)
    queues = []
    for queue in QUEUES:
        waits = _recent(client, _WAITS_KEY.format(queue=queue), now - window_seconds)
        runtimes = _recent(client, _RUNTIMES_KEY.format(queue=queue), now - window_seconds)
        wait_values = [w for _, w, _ in waits]
        runtime_values = [r for _, r, _ in runtimes]
        info = limits[queue]
        entry: dict[str, Any] = {
            "queue": queue,
            "service": info["service"],
            "rate_limit": info["description"],
            "configured_workers": info["configured_workers"],
            "waiting": queue_depth(client, queue),
            "running": running.get(queue, 0),
            "oldest_waiting_seconds": _oldest_waiting_age(client, queue, now),
            "started_in_window": len(waits),
            "avg_wait_seconds": round(statistics.fmean(wait_values), 1) if wait_values else None,
            "p95_wait_seconds": _p95(wait_values),
            "max_wait_seconds": round(max(wait_values), 1) if wait_values else None,
            "avg_runtime_seconds": round(statistics.fmean(runtime_values), 1) if runtime_values else None,
            "completed_in_window": len(runtimes),
            # Fair-share counters: outstanding (queued + running) per company.
            "outstanding_by_company": _outstanding_named(queue),
        }
        if info.get("limiter"):
            entry["rate_limiter"] = rate_limiter.stats(info["limiter"], info["period_seconds"])
        if info.get("token_limiter"):
            entry["token_rate_limiter"] = rate_limiter.stats(info["token_limiter"], 60.0)
        queues.append(entry)
    return {"available": True, "window_seconds": window_seconds, "generated_at": now, "queues": queues}


def _outstanding_named(queue: str) -> list[dict[str, Any]]:
    from app.tasks import fairshare

    counts = fairshare.outstanding(queue)
    if not counts:
        return []
    names: dict[str, str] = {}
    try:
        import uuid as _uuid

        from sqlalchemy import select

        from app.db.session import system_session
        from app.models.company import Company

        with system_session() as db:
            ids = [_uuid.UUID(c) for c in counts]
            names = {str(c.id): c.name for c in db.execute(select(Company).where(Company.id.in_(ids))).scalars()}
    except Exception:  # noqa: BLE001
        pass
    return sorted(
        ({"company_id": c, "company_name": names.get(c), "outstanding": n} for c, n in counts.items()),
        key=lambda r: -r["outstanding"],
    )


def workers_by_queue(timeout: float = 1.0) -> dict[str, list[str]] | None:
    """Which worker processes are consuming each queue (Celery remote control
    — needs the workers to answer within `timeout`). None if unavailable."""
    try:
        from app.tasks.celery_app import celery_app

        active = celery_app.control.inspect(timeout=timeout).active_queues() or {}
    except Exception:  # noqa: BLE001
        return None
    result: dict[str, list[str]] = {}
    for worker, queues in active.items():
        for q in queues or []:
            result.setdefault(q.get("name"), []).append(worker)
    return result
