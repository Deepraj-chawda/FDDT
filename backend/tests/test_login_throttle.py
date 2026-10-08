"""Sign-in throttling on POST /auth/login (app/services/login_throttle.py),
against the in-memory Redis from conftest's `login_redis` fixture."""
from app.core.config import settings
from app.services import login_throttle

GOOD = "TestPassword123!"


def _login(client, email, password, ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return client.post("/auth/login", json={"email": email, "password": password}, headers=headers)


def test_account_is_locked_after_max_failures_even_with_the_right_password(client, seeded_user, monkeypatch, login_redis):
    monkeypatch.setattr(settings, "login_max_failed_attempts", 3)
    monkeypatch.setattr(settings, "login_lockout_minutes", 15)
    for _ in range(3):
        assert _login(client, seeded_user.email, "wrong").status_code == 401

    blocked = _login(client, seeded_user.email, GOOD)
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == str(15 * 60)
    assert blocked.json()["detail"] == "Too many sign-in attempts. Try again in 15 minutes."


def test_success_clears_the_failure_count(client, seeded_user, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failed_attempts", 3)
    for _ in range(2):
        assert _login(client, seeded_user.email, "wrong").status_code == 401
    assert _login(client, seeded_user.email, GOOD).status_code == 200
    # The count restarted: two more failures are still allowed.
    for _ in range(2):
        assert _login(client, seeded_user.email, "wrong").status_code == 401
    assert _login(client, seeded_user.email, GOOD).status_code == 200


def test_unknown_addresses_are_counted_too_and_case_does_not_matter(client, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failed_attempts", 2)
    for _ in range(2):
        assert _login(client, "Nobody@Example.com", "x").status_code == 401
    assert _login(client, "nobody@example.com", "x").status_code == 429


def test_lockout_is_per_address(client, seeded_user, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failed_attempts", 2)
    for _ in range(2):
        _login(client, "attacker-target@example.com", "x")
    assert _login(client, seeded_user.email, GOOD).status_code == 200


def test_per_ip_limit(client, seeded_user, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failed_attempts", 0)  # isolate the IP limit
    monkeypatch.setattr(settings, "login_max_attempts_per_ip_per_minute", 3)
    for _ in range(3):
        assert _login(client, seeded_user.email, GOOD).status_code == 200
    over = _login(client, seeded_user.email, GOOD)
    assert over.status_code == 429
    assert int(over.headers["Retry-After"]) <= 60


def test_zero_disables_both_limits(client, seeded_user, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failed_attempts", 0)
    monkeypatch.setattr(settings, "login_max_attempts_per_ip_per_minute", 0)
    for _ in range(30):
        assert _login(client, seeded_user.email, "wrong").status_code == 401
    assert _login(client, seeded_user.email, GOOD).status_code == 200


def test_fails_open_when_redis_is_down(client, seeded_user, monkeypatch):
    def broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr(login_throttle, "_redis", broken)
    assert _login(client, seeded_user.email, "wrong").status_code == 401
    assert _login(client, seeded_user.email, GOOD).status_code == 200


def test_redis_keys_never_hold_the_email_in_clear(client, monkeypatch, login_redis):
    _login(client, "someone@example.com", "x")
    assert login_redis.values
    assert all("someone" not in key for key in login_redis.values)
