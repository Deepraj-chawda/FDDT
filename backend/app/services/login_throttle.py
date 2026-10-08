"""
Sign-in throttling (POST /auth/login), kept in the broker Redis so every API
replica shares the counters.

* Per account: after LOGIN_MAX_FAILED_ATTEMPTS failed sign-ins for one email
  address (default 5), that address is refused for LOGIN_LOCKOUT_MINUTES
  (default 15), counted from the first failure of the window. This is the
  real brute-force protection: it holds whatever IPs the attempts come from.
  A successful sign-in clears the count. Unknown addresses are counted the
  same way, so the response never reveals whether an account exists.
* Per client IP: at most LOGIN_MAX_ATTEMPTS_PER_IP_PER_MINUTE attempts of any
  outcome (default 20) — a cheap brake on password spraying across many
  accounts. The IP is what uvicorn reports after --proxy-headers; behind the
  proxy chain a client can influence X-Forwarded-For, so this limit is
  best-effort and the per-account limit is the one relied on.

Fails open: if Redis is unreachable, sign-in is not blocked (availability over
throttling; the outage is logged).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from functools import lru_cache

from app.core.config import settings

logger = logging.getLogger("fddt.login_throttle")

_ACCOUNT_KEY = "fddt:login:fail:{digest}"
_IP_KEY = "fddt:login:ip:{ip}"


@lru_cache
def _redis():
    import redis

    return redis.Redis.from_url(settings.celery_broker_url, socket_connect_timeout=0.5, socket_timeout=1)


def _account_key(email: str) -> str:
    # Hashed: the key space never holds email addresses in clear text.
    return _ACCOUNT_KEY.format(digest=hashlib.sha256(email.strip().lower().encode()).hexdigest())


@dataclass(frozen=True)
class Blocked:
    retry_after_seconds: int
    reason: str  # "account" | "ip"


def check(email: str, client_ip: str | None) -> Blocked | None:
    """Call before verifying the password. Counts this attempt against the
    IP; returns why the attempt must be refused, or None."""
    try:
        client = _redis()
        max_failed = settings.login_max_failed_attempts
        if max_failed > 0:
            key = _account_key(email)
            failures = int(client.get(key) or 0)
            if failures >= max_failed:
                return Blocked(max(1, int(client.ttl(key))), "account")
        per_ip = settings.login_max_attempts_per_ip_per_minute
        if per_ip > 0 and client_ip:
            key = _IP_KEY.format(ip=client_ip)
            attempts = int(client.incr(key))
            if attempts == 1:
                client.expire(key, 60)
            if attempts > per_ip:
                return Blocked(max(1, int(client.ttl(key))), "ip")
    except Exception:  # noqa: BLE001
        logger.warning("login throttle unavailable (Redis); not throttling", exc_info=True)
    return None


def record_failure(email: str) -> None:
    if settings.login_max_failed_attempts <= 0:
        return
    try:
        client = _redis()
        key = _account_key(email)
        failures = int(client.incr(key))
        if failures == 1:
            client.expire(key, max(1, settings.login_lockout_minutes) * 60)
        if failures == settings.login_max_failed_attempts:
            logger.warning("login_lockout account=%s… failures=%d", key.rsplit(":", 1)[-1][:12], failures)
    except Exception:  # noqa: BLE001
        logger.warning("login throttle unavailable (Redis); failure not counted", exc_info=True)


def record_success(email: str) -> None:
    try:
        _redis().delete(_account_key(email))
    except Exception:  # noqa: BLE001
        pass
