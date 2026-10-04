"""
Global rate limiter (app/services/rate_limiter.py) against a real Redis —
the limiter's whole point is shared state across processes, which a mock
can't show. Skipped when the broker Redis isn't reachable.
"""
import threading
import time
import uuid

import pytest

from app.services import rate_limiter


def _redis_available() -> bool:
    try:
        rate_limiter._redis().ping()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _redis_available(), reason="broker Redis not reachable")


@pytest.fixture(autouse=True)
def _cleanup_test_keys():
    yield
    client = rate_limiter._redis()
    for key in client.scan_iter("fddt:ratelimit:test_*"):
        client.delete(key)


def _hammer(name, threads, calls_each, **acquire_kwargs):
    starts: list[float] = []
    lock = threading.Lock()

    def worker():
        for _ in range(calls_each):
            rate_limiter.acquire(name, **acquire_kwargs)
            with lock:
                starts.append(time.monotonic())

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for t in pool:
        t.start()
    for t in pool:
        t.join()
    return sorted(starts)


def _max_in_any_window(starts, window):
    """Max calls observed in any `window` seconds. Measured when acquire()
    returns, i.e. a few ms after the limiter admitted the call, so callers use
    a window slightly shorter than the limiter's to absorb that jitter."""
    best = 0
    for i, s in enumerate(starts):
        j = i
        while j < len(starts) and starts[j] - s < window:
            j += 1
        best = max(best, j - i)
    return best


def test_calls_per_second_cap_holds_across_concurrent_callers():
    name = f"test_{uuid.uuid4().hex}"
    starts = _hammer(name, threads=8, calls_each=4, max_units=5, period_seconds=1.0)
    assert len(starts) == 32
    # 32 calls at 5/s must take ~6 s, and never exceed 5 in any 1 s window.
    assert starts[-1] - starts[0] >= 5.5
    assert _max_in_any_window(starts, 0.95) <= 5


def test_weighted_cost_cap_tokens_per_window():
    name = f"test_{uuid.uuid4().hex}"
    # 1,000 "tokens" per second, each call costs 400: at most 2 per second.
    starts = _hammer(name, threads=4, calls_each=2, max_units=1000, period_seconds=1.0, cost=400)
    assert len(starts) == 8
    assert _max_in_any_window(starts, 0.95) <= 2
    assert starts[-1] - starts[0] >= 2.5


def test_azure_429_pauses_every_caller():
    name = f"test_{uuid.uuid4().hex}"
    rate_limiter.report_throttled(name, 1.5)
    t0 = time.monotonic()
    rate_limiter.acquire(name, max_units=100, period_seconds=1.0)
    assert time.monotonic() - t0 >= 1.2
    assert rate_limiter.stats(name, 1.0)["http_429_total"] == 1


def test_retry_after_parsing():
    class Resp:
        headers = {"retry-after-ms": "2500"}

    class Err(Exception):
        response = Resp()

    assert rate_limiter.retry_after_from(Err()) == 2.5
    Resp.headers = {"retry-after": "7"}
    assert rate_limiter.retry_after_from(Err()) == 7.0
