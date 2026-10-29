"""Sessions: short-lived access token + rotating, httpOnly refresh-token
cookie (app/api/auth.py, app/services/refresh_tokens.py)."""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from sqlalchemy import select

from app.core import security
from app.core.config import settings
from app.db import tenancy
from app.models.audit_log import AuditLog
from app.models.refresh_token import RefreshToken

PASSWORD = "TestPassword123!"
CSRF = {"X-Requested-With": "fddt-web"}
COOKIE = settings.refresh_cookie_name


@pytest.fixture(autouse=True)
def _plain_http_cookies(monkeypatch):
    """The test client talks plain http to `testserver` without the /api
    prefix: let it keep and send the cookie."""
    monkeypatch.setattr(settings, "refresh_cookie_secure", False)
    monkeypatch.setattr(settings, "refresh_cookie_path", "/")


def _login(client, user):
    res = client.post("/auth/login", json={"email": user.email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return res


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _refresh(client, cookie=None):
    if cookie is not None:
        client.cookies.clear()
        client.cookies.set(COOKIE, cookie)
    return client.post("/auth/refresh", headers=CSRF)


def _platform_rows(db_session, model, *where):
    state = tenancy.snapshot(db_session)
    tenancy.bind_platform(db_session)
    try:
        db_session.expire_all()
        return db_session.execute(select(model).where(*where)).scalars().all()
    finally:
        tenancy.restore(db_session, state)


def test_login_returns_short_access_token_and_httponly_refresh_cookie(client, seeded_user):
    res = _login(client, seeded_user)
    body = res.json()
    assert set(body) == {"access_token", "token_type", "expires_in"}  # refresh token never in the body
    assert body["expires_in"] == settings.jwt_access_token_expire_minutes * 60 <= 3600
    cookie = res.headers["set-cookie"].lower()
    assert f"{COOKIE}=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert res.headers["cache-control"] == "no-store"
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert {"iss", "aud", "jti", "typ", "ver", "nbf"} <= set(claims) and claims["typ"] == "access"
    assert client.get("/auth/me", headers=_bearer(body["access_token"])).status_code == 200


def test_refresh_rotates_and_needs_the_csrf_header(client, seeded_user):
    _login(client, seeded_user)
    first = client.cookies.get(COOKIE)
    assert client.post("/auth/refresh").status_code == 403  # no X-Requested-With
    res = _refresh(client)
    assert res.status_code == 200
    second = client.cookies.get(COOKIE)
    assert second and second != first
    assert client.get("/auth/me", headers=_bearer(res.json()["access_token"])).status_code == 200


def test_reusing_a_rotated_token_ends_the_whole_sign_in(client, db_session, seeded_user, monkeypatch):
    monkeypatch.setattr(settings, "refresh_token_reuse_grace_seconds", 0)
    _login(client, seeded_user)
    stolen = client.cookies.get(COOKIE)
    assert _refresh(client).status_code == 200
    current = client.cookies.get(COOKIE)

    replay = _refresh(client, stolen)
    assert replay.status_code == 401
    assert f'{COOKIE}=""' in replay.headers["set-cookie"] or "max-age=0" in replay.headers["set-cookie"].lower()
    # The legitimate holder's newer token is dead too: the family is revoked.
    assert _refresh(client, current).status_code == 401
    rows = _platform_rows(db_session, RefreshToken, RefreshToken.user_id == seeded_user.id)
    assert {r.revoked_reason for r in rows} >= {"reuse_detected"} and all(r.revoked_at for r in rows)
    events = _platform_rows(db_session, AuditLog, AuditLog.event_type == "refresh_token_reuse_detected")
    assert len(events) == 1 and events[0].actor_user_id == seeded_user.id


def test_two_tabs_refreshing_at_once_is_not_theft(client, seeded_user):
    _login(client, seeded_user)
    shared = client.cookies.get(COOKIE)
    assert _refresh(client).status_code == 200
    newer = client.cookies.get(COOKIE)
    late = _refresh(client, shared)  # the second tab, inside the grace window
    assert late.status_code == 200 and "set-cookie" not in late.headers
    assert _refresh(client, newer).status_code == 200  # the session lives on


def test_unknown_missing_and_expired_tokens(client, db_session, seeded_user):
    assert _refresh(client, "not-a-real-token").status_code == 401
    client.cookies.clear()
    assert client.post("/auth/refresh", headers=CSRF).status_code == 401
    _login(client, seeded_user)
    state = tenancy.snapshot(db_session)
    tenancy.bind_platform(db_session)
    for row in db_session.execute(select(RefreshToken)).scalars():
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    tenancy.restore(db_session, state)
    assert _refresh(client).status_code == 401


def test_logout_ends_the_session(client, seeded_user):
    _login(client, seeded_user)
    token = client.cookies.get(COOKIE)
    assert client.post("/auth/logout").status_code == 403  # CSRF header required
    assert client.post("/auth/logout", headers=CSRF).status_code == 204
    assert _refresh(client, token).status_code == 401


def test_logout_everywhere_kills_access_and_refresh_tokens(client, seeded_user):
    first = _login(client, seeded_user)
    first_cookie = client.cookies.get(COOKIE)
    second = _login(client, seeded_user)
    access = second.json()["access_token"]
    assert client.post("/auth/logout-all", headers=_bearer(access)).status_code == 204
    assert client.get("/auth/me", headers=_bearer(access)).status_code == 401
    assert client.get("/auth/me", headers=_bearer(first.json()["access_token"])).status_code == 401
    assert _refresh(client, first_cookie).status_code == 401


def test_admin_password_reset_and_deactivation_end_sessions(client, plain_user, platform_admin_headers):
    res = _login(client, plain_user)
    cookie = client.cookies.get(COOKIE)
    reset = client.post(
        f"/settings/users/{plain_user.id}/reset-password", headers=platform_admin_headers,
        json={"password": "AnotherPass123!"},
    )
    assert reset.status_code == 200, reset.text
    assert client.get("/auth/me", headers=_bearer(res.json()["access_token"])).status_code == 401
    assert _refresh(client, cookie).status_code == 401

    client.cookies.clear()
    login = client.post("/auth/login", json={"email": plain_user.email, "password": "AnotherPass123!"})
    cookie = client.cookies.get(COOKIE)
    client.patch(f"/settings/users/{plain_user.id}", headers=platform_admin_headers, json={"is_active": False})
    assert client.get("/auth/me", headers=_bearer(login.json()["access_token"])).status_code == 401
    assert _refresh(client, cookie).status_code == 401


def test_only_genuine_access_tokens_are_accepted(client, seeded_user):
    good = jwt.decode(_login(client, seeded_user).json()["access_token"], options={"verify_signature": False})
    refresh_cookie = client.cookies.get(COOKIE)

    def signed(claims, key=settings.jwt_secret_key, alg=settings.jwt_algorithm):
        return jwt.encode(claims, key, algorithm=alg)

    rejected = [
        refresh_cookie,  # a refresh token is not a bearer token
        signed({**good, "typ": "refresh"}),
        signed({**good, "aud": "someone-else"}),
        signed({**good, "iss": "someone-else"}),
        signed({k: v for k, v in good.items() if k != "ver"}),
        signed({**good, "exp": int(datetime.now(timezone.utc).timestamp()) - 60}),
        signed(good, key="x" * 40),  # wrong key
        jwt.encode(good, None, algorithm="none"),  # unsigned
    ]
    for token in rejected:
        assert client.get("/auth/me", headers=_bearer(token)).status_code == 401, token


def test_unknown_email_and_wrong_password_look_the_same(client, seeded_user):
    a = client.post("/auth/login", json={"email": "nobody@example.com", "password": "x"})
    b = client.post("/auth/login", json={"email": seeded_user.email, "password": "wrong"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    assert "set-cookie" not in a.headers and "set-cookie" not in b.headers


def test_sign_in_is_audited(client, db_session, seeded_user):
    _login(client, seeded_user)
    (event,) = _platform_rows(db_session, AuditLog, AuditLog.event_type == "user_signed_in")
    assert event.actor_user_id == seeded_user.id and PASSWORD not in str(event.event_data)


def test_security_headers(client, seeded_user):
    res = client.get("/health")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["referrer-policy"] == "no-referrer"


def test_weak_signing_key_refused_outside_local(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "jwt_secret_key", "changeme-in-.env")
    with pytest.raises(RuntimeError):
        security.check_secret_strength()
    monkeypatch.setattr(settings, "jwt_secret_key", "short")
    with pytest.raises(RuntimeError):
        security.check_secret_strength()
    monkeypatch.setattr(settings, "jwt_secret_key", "k" * 48)
    security.check_secret_strength()
