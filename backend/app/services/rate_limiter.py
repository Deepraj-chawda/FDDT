"""
Global (cross-worker) rate limiting for the external Azure APIs.

Celery's own `rate_limit` task option is enforced per worker process, so it
stops being a ceiling the moment a second worker container starts. This
limiter keeps its state in Redis instead, so the ceiling holds no matter how
many extraction/vision workers are running:

* `acquire(name, max_units, period, cost=1)` — a weighted sliding-window log
  (Redis sorted set, one atomic Lua script): the costs of the calls started in
  any `period` seconds never exceed `max_units`, across every process. With
  cost=1 that is a calls-per-period cap (Document Intelligence TPS, Azure
  OpenAI RPM); with cost=estimated tokens it is a tokens-per-minute cap
  (Azure OpenAI TPM — Azure counts a request's prompt AND its max_tokens
  against TPM when it arrives, so a vision call reserving 4,000 output tokens
  costs ~5,000 of the quota even if it answers in 300). A caller over the
  limit sleeps until enough of the window has expired, then tries again.
* `report_throttled(name, retry_after)` — when Azure still answers 429 (the
  quota is shared with something else, or the configured ceiling is too
  high), every worker pauses calls to that service for the Retry-After period
  instead of each one hammering it independently.

It is applied at the call sites (app/services/ocr_service.py,
app/services/llm_service.py), so it limits actual API calls — one vision task
can make several — not tasks.

The forensics queue has no limiter at all: it only uses local CPU.

If Redis is unreachable the limiter logs and lets calls through (the SDKs' own
429 retry/back-off is then the only protection) rather than stalling the whole
pipeline.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from functools import lru_cache

from app.core.config import settings

logger = logging.getLogger("fddt.rate_limiter")

AZURE_DOCUMENT_INTELLIGENCE = "azure_document_intelligence"
AZURE_OPENAI = "azure_openai"
AZURE_OPENAI_TOKENS = "azure_openai_tokens"

_KEY = "fddt:ratelimit:{name}"
_COOLDOWN_KEY = "fddt:ratelimit:{name}:cooldown"
_STATS_KEY = "fddt:ratelimit:{name}:stats"

# Members are "<cost>:<uuid>" scored by start time. Returns '0' when the call
# may proceed (and records it), otherwise the seconds to wait (as a string).
_SLIDING_WINDOW = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local cost = tonumber(ARGV[5])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local entries = redis.call('ZRANGE', key, 0, -1, 'WITHSCORES')
local used = 0
for i = 1, #entries, 2 do
  used = used + tonumber(string.match(entries[i], '^([^:]+)'))
end
if used + cost <= limit or #entries == 0 then
  redis.call('ZADD', key, now, cost .. ':' .. ARGV[4])
  redis.call('PEXPIRE', key, math.ceil(window * 1000) + 1000)
  return '0'
end
local need = used + cost - limit
local freed = 0
for i = 1, #entries, 2 do
  freed = freed + tonumber(string.match(entries[i], '^([^:]+)'))
  if freed >= need then
    return tostring(tonumber(entries[i + 1]) + window - now)
  end
end
return tostring(window)
"""

_unavailable_until = 0.0
_lock = threading.Lock()


@lru_cache
def _redis():
    import redis

    return redis.Redis.from_url(
        settings.celery_broker_url, socket_connect_timeout=0.5, socket_timeout=2
    )


def _client():
    global _unavailable_until
    if time.monotonic() < _unavailable_until:
        return None
    try:
        client = _redis()
        client.ping()
        return client
    except Exception:  # noqa: BLE001
        with _lock:
            _unavailable_until = time.monotonic() + 30
        logger.warning("Rate limiter: Redis unavailable; calls are not being rate limited", exc_info=True)
        return None


def acquire(
    name: str,
    max_units: float,
    period_seconds: float,
    *,
    cost: int = 1,
    max_wait_seconds: float = 600,
    cooldown_name: str | None = None,
) -> float:
    """Block until a call to `name` costing `cost` units fits under
    `max_units` per `period_seconds` (across every worker). Returns how long
    it waited. `max_units <= 0` disables the limit. `cooldown_name` is the
    service whose Azure-429 pause also applies (defaults to `name`)."""
    if max_units <= 0:
        return 0.0
    client = _client()
    if client is None:
        return 0.0
    limit = max(1, int(max_units))
    cost = max(1, min(int(cost), limit))
    started = time.monotonic()
    member = uuid.uuid4().hex
    script = client.register_script(_SLIDING_WINDOW)
    waited_for_limit = False
    while True:
        try:
            cooldown = client.pttl(_COOLDOWN_KEY.format(name=cooldown_name or name))
            if cooldown and cooldown > 0:
                waited_for_limit = True
                time.sleep(min(cooldown / 1000.0, 30))
                continue
            wait = float(
                script(keys=[_KEY.format(name=name)], args=[time.time(), period_seconds, limit, member, cost])
            )
        except Exception:  # noqa: BLE001
            logger.warning("Rate limiter: Redis error; letting the call through", exc_info=True)
            return time.monotonic() - started
        if wait <= 0:
            waited = time.monotonic() - started
            try:
                pipe = client.pipeline()
                pipe.hincrby(_STATS_KEY.format(name=name), "calls", 1)
                if waited_for_limit:
                    pipe.hincrby(_STATS_KEY.format(name=name), "throttled_calls", 1)
                    pipe.hincrbyfloat(_STATS_KEY.format(name=name), "throttled_seconds", waited)
                pipe.execute()
            except Exception:  # noqa: BLE001
                pass
            if waited_for_limit:
                logger.info("rate_limit name=%s waited=%.2fs", name, waited)
            return waited
        waited_for_limit = True
        if time.monotonic() - started + wait > max_wait_seconds:
            raise TimeoutError(f"Rate limiter: waited over {max_wait_seconds}s for {name}")
        time.sleep(min(wait + 0.01, 30))


def report_throttled(name: str, retry_after_seconds: float | None) -> None:
    """Azure answered 429: pause every worker's calls to `name`."""
    pause = retry_after_seconds if retry_after_seconds and retry_after_seconds > 0 else 10.0
    logger.warning("rate_limit name=%s azure_429 pausing_all_workers=%.1fs", name, pause)
    client = _client()
    if client is None:
        return
    try:
        client.set(_COOLDOWN_KEY.format(name=name), "1", px=int(pause * 1000))
        client.hincrby(_STATS_KEY.format(name=name), "http_429", 1)
    except Exception:  # noqa: BLE001
        pass


def stats(name: str, period_seconds: float) -> dict:
    """Counters for the queue monitor."""
    client = _client()
    if client is None:
        return {"available": False}
    try:
        now = time.time()
        members = client.zrangebyscore(_KEY.format(name=name), now - period_seconds, "+inf")
        in_window = sum(int(float(m.decode().split(":", 1)[0])) for m in members)
        raw = client.hgetall(_STATS_KEY.format(name=name))
        cooldown_ms = client.pttl(_COOLDOWN_KEY.format(name=name))
    except Exception:  # noqa: BLE001
        return {"available": False}
    decoded = {k.decode(): float(v) for k, v in raw.items()}
    return {
        "available": True,
        "units_in_current_window": int(in_window),
        "calls_total": int(decoded.get("calls", 0)),
        "throttled_calls_total": int(decoded.get("throttled_calls", 0)),
        "throttled_seconds_total": round(decoded.get("throttled_seconds", 0.0), 1),
        "http_429_total": int(decoded.get("http_429", 0)),
        "cooldown_remaining_seconds": round(cooldown_ms / 1000.0, 1) if cooldown_ms and cooldown_ms > 0 else 0,
    }


def retry_after_from(exc: Exception) -> float | None:
    """Best-effort Retry-After (seconds) from an SDK exception's response."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    for header in ("retry-after-ms", "x-ms-retry-after-ms", "retry-after"):
        value = headers.get(header)
        if value is None:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        return seconds / 1000.0 if header.endswith("-ms") else seconds
    return None
