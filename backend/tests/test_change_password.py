"""Self-service password change (POST /auth/me/password): every signed-in
user, with their current password. A forgotten password is reset by a
platform admin instead (POST /settings/users/{id}/reset-password)."""
from __future__ import annotations

from sqlalchemy import select

from app.db import tenancy
from app.models.audit_log import AuditLog

OLD = "TestPassword123!"


def _token(client, email: str, password: str = OLD) -> str:
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _change(client, token: str, current: str, new: str):
    return client.post(
        "/auth/me/password",
        json={"current_password": current, "new_password": new},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_user_changes_own_password_and_signs_in_with_it(client, seeded_user, db_session):
    token = _token(client, seeded_user.email)
    response = _change(client, token, OLD, "BrandNewPass9")
    assert response.status_code == 204
    # The old password no longer works; the new one does.
    assert client.post("/auth/login", json={"email": seeded_user.email, "password": OLD}).status_code == 401
    _token(client, seeded_user.email, "BrandNewPass9")
    # Audited, without the password.
    state = tenancy.snapshot(db_session)
    tenancy.bind_platform(db_session)
    try:
        [event] = db_session.execute(
            select(AuditLog).where(AuditLog.event_type == "user_password_changed")
        ).scalars().all()
    finally:
        tenancy.restore(db_session, state)
    assert event.actor_user_id == seeded_user.id
    assert "BrandNewPass9" not in str(event.event_data) and OLD not in str(event.event_data)


def test_wrong_current_password_is_rejected(client, seeded_user):
    token = _token(client, seeded_user.email)
    response = _change(client, token, "not-my-password", "BrandNewPass9")
    assert response.status_code == 400
    assert response.json()["detail"] == "Current password is incorrect."
    _token(client, seeded_user.email)  # unchanged


def test_new_password_must_differ_and_be_long_enough(client, seeded_user):
    token = _token(client, seeded_user.email)
    assert _change(client, token, OLD, OLD).status_code == 400
    assert _change(client, token, OLD, "short").status_code == 422


def test_requires_sign_in(client):
    response = client.post("/auth/me/password", json={"current_password": OLD, "new_password": "BrandNewPass9"})
    assert response.status_code == 401


def test_platform_admin_can_change_own_password(client, platform_admin_user):
    token = _token(client, platform_admin_user.email)
    assert _change(client, token, OLD, "AdminNewPass9").status_code == 204
    _token(client, platform_admin_user.email, "AdminNewPass9")
