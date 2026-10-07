"""Admin Settings API: issuers, versioned risk rules + thresholds, users, audit."""
import pytest
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.issuer_registry import IssuerRegistry, IssuerType
from app.models.risk_rule import RiskRule
from app.models.user import User, UserRole
from app.services.issuer_service import match_issuer
from app.services.risk_rule_seed import seed_risk_rules


@pytest.fixture()
def seeded_rules(db_session):
    seed_risk_rules(db_session, db_session.info["test_company_id"])
    db_session.commit()


def _events(db, event_type):
    return db.execute(select(AuditLog).where(AuditLog.event_type == event_type).order_by(AuditLog.created_at)).scalars().all()


# --------------------------------------------------------------- access control

ADMIN_ENDPOINTS = [
    ("get", "/settings/issuers", None),
    ("post", "/settings/issuers", {"name": "X"}),
    ("patch", "/settings/issuers/00000000-0000-0000-0000-000000000000", {"is_active": False}),
    ("get", "/settings/risk-rules", None),
    ("get", "/settings/risk-rules/ela.tamper_region_detected/history", None),
    ("patch", "/settings/risk-rules/ela.tamper_region_detected", {"weight": 1}),
    ("get", "/settings/risk-thresholds", None),
    ("put", "/settings/risk-thresholds", {"medium_threshold": 20, "high_threshold": 50}),
    ("get", "/settings/users", None),
    ("patch", "/settings/users/00000000-0000-0000-0000-000000000000", {"is_active": False}),
    ("post", "/settings/users/00000000-0000-0000-0000-000000000000/reset-password", {"password": "NewSecret123!"}),
]


@pytest.mark.parametrize("method,path,body", ADMIN_ENDPOINTS)
def test_settings_require_authentication(client, method, path, body):
    assert getattr(client, method)(path, **({"json": body} if body is not None else {})).status_code == 401


@pytest.mark.parametrize("method,path,body", ADMIN_ENDPOINTS)
def test_non_admins_get_403_on_every_settings_endpoint(client, reviewer_headers, plain_headers, method, path, body):
    for headers in (reviewer_headers, plain_headers):
        kwargs = {"headers": headers, **({"json": body} if body is not None else {})}
        assert getattr(client, method)(path, **kwargs).status_code == 403


# ---------------------------------------------------------------------- issuers

def test_issuer_crud_and_soft_delete(client, auth_headers, db_session):
    created = client.post("/settings/issuers", headers=auth_headers, json={
        "name": "  Al Nukhba Technical Systems ", "name_arabic": "النخبة للأنظمة الفنية", "tax_id": "TX-1", "type": "vendor",
    })
    assert created.status_code == 201
    issuer = created.json()
    assert issuer["name"] == "Al Nukhba Technical Systems"
    assert issuer["name_arabic"] == "النخبة للأنظمة الفنية"
    assert issuer["is_active"] is True and issuer["type"] == "vendor"

    edited = client.patch(f"/settings/issuers/{issuer['id']}", headers=auth_headers,
                          json={"tax_id": "TX-2", "type": "other", "name_arabic": ""})
    assert edited.status_code == 200
    assert edited.json()["tax_id"] == "TX-2" and edited.json()["name_arabic"] is None

    off = client.patch(f"/settings/issuers/{issuer['id']}", headers=auth_headers, json={"is_active": False})
    assert off.json()["is_active"] is False

    listed = client.get("/settings/issuers", headers=auth_headers).json()
    assert [i["id"] for i in listed] == [issuer["id"]]  # soft delete: still listed
    assert client.get("/settings/issuers?include_inactive=false", headers=auth_headers).json() == []
    assert db_session.get(IssuerRegistry, __import__("uuid").UUID(issuer["id"])) is not None  # never hard-deleted

    on = client.patch(f"/settings/issuers/{issuer['id']}", headers=auth_headers, json={"is_active": True})
    assert on.json()["is_active"] is True
    kinds = [e.event_type for e in db_session.query(AuditLog).order_by(AuditLog.created_at)]
    assert kinds == ["issuer_created", "issuer_updated", "issuer_deactivated", "issuer_reactivated"]


def test_issuer_validation_and_duplicates(client, auth_headers):
    assert client.post("/settings/issuers", headers=auth_headers, json={"name": "   "}).status_code == 422
    assert client.post("/settings/issuers", headers=auth_headers, json={"name": "Acme"}).status_code == 201
    assert client.post("/settings/issuers", headers=auth_headers, json={"name": "ACME"}).status_code == 409
    assert client.patch("/settings/issuers/00000000-0000-0000-0000-000000000000", headers=auth_headers,
                        json={"name": "Z"}).status_code == 404


def test_deactivated_issuer_no_longer_verifies(client, auth_headers, db_session):
    issuer = client.post("/settings/issuers", headers=auth_headers, json={"name": "Globex Supplies", "type": "vendor"}).json()
    assert match_issuer(db_session, db_session.info["test_company_id"], "Globex Supplies").matched is True
    client.patch(f"/settings/issuers/{issuer['id']}", headers=auth_headers, json={"is_active": False})
    assert match_issuer(db_session, db_session.info["test_company_id"], "Globex Supplies").matched is False


# -------------------------------------------------------------------- risk rules

def test_list_rules_returns_current_versions(client, auth_headers, seeded_rules):
    rules = client.get("/settings/risk-rules", headers=auth_headers).json()
    assert len(rules) >= 30
    ela = next(r for r in rules if r["rule_id"] == "ela.tamper_region_detected")
    assert (ela["weight"], ela["severity"], ela["version"], ela["is_active"]) == (25, "medium", 1, True)
    assert "{count}" in ela["reason_template"]
    assert {r["category"] for r in rules} == {"forensics", "consistency", "verification", "duplication"}


def test_editing_a_rule_creates_a_new_version_and_leaves_the_old_row_alone(client, auth_headers, db_session, seeded_rules, seeded_user):
    before = db_session.execute(select(RiskRule).where(RiskRule.rule_id == "ela.tamper_region_detected")).scalars().one()
    before_snapshot = (before.id, before.weight, before.severity, before.version, before.updated_at)

    res = client.patch("/settings/risk-rules/ela.tamper_region_detected", headers=auth_headers,
                       json={"weight": 12.5, "severity": "low", "change_note": "Too noisy on scans"})
    assert res.status_code == 200
    new = res.json()
    assert (new["weight"], new["severity"], new["version"]) == (12.5, "low", 2)
    assert new["updated_by_name"] == seeded_user.full_name and new["change_note"] == "Too noisy on scans"
    assert new["id"] != str(before.id)

    db_session.expire_all()
    old = db_session.get(RiskRule, before.id)
    assert (old.id, old.weight, old.severity, old.version, old.updated_at) == before_snapshot  # untouched

    current = next(r for r in client.get("/settings/risk-rules", headers=auth_headers).json() if r["rule_id"] == "ela.tamper_region_detected")
    assert current["version"] == 2 and current["weight"] == 12.5

    history = client.get("/settings/risk-rules/ela.tamper_region_detected/history", headers=auth_headers).json()
    assert [(h["version"], h["weight"]) for h in history] == [(2, 12.5), (1, 25)]
    assert history[0]["updated_by_name"] == seeded_user.full_name and history[1]["updated_by_name"] is None

    (event,) = _events(db_session, "risk_rule_updated")
    assert event.actor_user_id == seeded_user.id
    assert event.event_data["changes"]["weight"] == {"from": 25, "to": 12.5}
    assert event.event_data["from_version"] == 1 and event.event_data["to_version"] == 2


def test_toggle_active_is_a_versioned_edit(client, auth_headers, seeded_rules):
    res = client.patch("/settings/risk-rules/copy_move.cluster_detected", headers=auth_headers, json={"is_active": False})
    assert res.json()["is_active"] is False and res.json()["version"] == 2
    history = client.get("/settings/risk-rules/copy_move.cluster_detected/history", headers=auth_headers).json()
    assert [h["is_active"] for h in history] == [False, True]


def test_rule_edit_validation(client, auth_headers, seeded_rules):
    url = "/settings/risk-rules/ela.tamper_region_detected"
    assert client.patch(url, headers=auth_headers, json={}).status_code == 422
    assert client.patch(url, headers=auth_headers, json={"weight": 500}).status_code == 422
    assert client.patch(url, headers=auth_headers, json={"severity": "critical"}).status_code == 422
    assert client.patch(url, headers=auth_headers, json={"weight": 25}).status_code == 400  # no actual change
    assert client.patch("/settings/risk-rules/nope.nope", headers=auth_headers, json={"weight": 1}).status_code == 404
    assert client.get("/settings/risk-rules/nope.nope/history", headers=auth_headers).status_code == 404


def test_thresholds_get_update_validate_and_audit(client, auth_headers, db_session):
    default = client.get("/settings/risk-thresholds", headers=auth_headers).json()
    assert (default["medium_threshold"], default["high_threshold"]) == (30, 60)

    res = client.put("/settings/risk-thresholds", headers=auth_headers, json={"medium_threshold": 25, "high_threshold": 55})
    assert res.status_code == 200 and res.json()["high_threshold"] == 55
    assert client.get("/settings/risk-thresholds", headers=auth_headers).json()["medium_threshold"] == 25

    assert client.put("/settings/risk-thresholds", headers=auth_headers, json={"medium_threshold": 60, "high_threshold": 30}).status_code == 422
    assert client.put("/settings/risk-thresholds", headers=auth_headers, json={"medium_threshold": 0, "high_threshold": 30}).status_code == 422
    (event,) = _events(db_session, "risk_thresholds_updated")
    assert event.event_data == {
        "from": {"medium": 30, "high": 60, "metadata_cap": 40},
        "to": {"medium": 25, "high": 55, "metadata_cap": 40},
    }


def test_metadata_score_cap_defaults_to_40_and_is_kept_unless_sent(client, auth_headers):
    assert client.get("/settings/risk-thresholds", headers=auth_headers).json()["metadata_score_cap"] == 40
    body = {"medium_threshold": 30, "high_threshold": 60}
    res = client.put("/settings/risk-thresholds", headers=auth_headers, json={**body, "metadata_score_cap": 55})
    assert res.status_code == 200 and res.json()["metadata_score_cap"] == 55
    assert client.put("/settings/risk-thresholds", headers=auth_headers, json=body).json()["metadata_score_cap"] == 55
    bad = {**body, "metadata_score_cap": 0}
    assert client.put("/settings/risk-thresholds", headers=auth_headers, json=bad).status_code == 422


# ------------------------------------------------------------------------ users
# User management is platform-admin only (companies cannot manage users).

def test_list_users_and_change_role_and_deactivate(
    client, platform_admin_headers, db_session, reviewer_user, plain_user, seeded_user
):
    users = client.get("/settings/users", headers=platform_admin_headers).json()
    assert {u["email"] for u in users} >= {"reviewer@example.com", "submitter@example.com", "test.user@example.com"}
    assert {u["company_name"] for u in users if u["email"] == "reviewer@example.com"} == {"Test Company"}

    res = client.patch(f"/settings/users/{plain_user.id}", headers=platform_admin_headers, json={"role": "reviewer_l2"})
    assert res.status_code == 200 and res.json()["role"] == "reviewer_l2"
    db_session.expire_all()
    assert db_session.get(User, plain_user.id).role == UserRole.reviewer_l2

    res = client.patch(f"/settings/users/{reviewer_user.id}", headers=platform_admin_headers, json={"is_active": False})
    assert res.json()["is_active"] is False
    db_session.expire_all()
    assert db_session.get(User, reviewer_user.id).is_active is False

    events = _events(db_session, "user_updated")
    assert [e.event_data["changes"] for e in events] == [
        {"role": {"from": "user", "to": "reviewer_l2"}},
        {"is_active": {"from": True, "to": False}},
    ]


def test_company_roles_cannot_manage_users(client, auth_headers, reviewer_headers, plain_headers, plain_user):
    """Not even a company's reviewer_l2 reaches Settings > Users."""
    for headers in (auth_headers, reviewer_headers, plain_headers):
        assert client.get("/settings/users", headers=headers).status_code == 403
        assert client.post(
            "/settings/users", headers=headers, json={"email": "x@example.com", "password": "S3cure-pass!"}
        ).status_code == 403
        assert client.patch(f"/settings/users/{plain_user.id}", headers=headers, json={"role": "reviewer_l2"}).status_code == 403


def test_deactivated_user_is_locked_out_and_can_be_reactivated(client, platform_admin_headers, reviewer_user, reviewer_headers):
    assert client.get("/cases", headers=reviewer_headers).status_code == 200
    client.patch(f"/settings/users/{reviewer_user.id}", headers=platform_admin_headers, json={"is_active": False})
    assert client.get("/cases", headers=reviewer_headers).status_code == 401
    client.patch(f"/settings/users/{reviewer_user.id}", headers=platform_admin_headers, json={"is_active": True})
    assert client.get("/cases", headers=reviewer_headers).status_code == 200


def test_role_change_takes_effect_immediately_not_at_token_expiry(client, platform_admin_headers, plain_user, plain_headers):
    """The JWT carries a role claim, but authorization re-reads the DB."""
    assert client.get("/settings/issuers", headers=plain_headers).status_code == 403
    client.patch(f"/settings/users/{plain_user.id}", headers=platform_admin_headers, json={"role": "reviewer_l2"})
    assert client.get("/settings/issuers", headers=plain_headers).status_code == 200


def test_moving_a_user_to_another_company_invalidates_their_token(
    client, db_session, platform_admin_headers, reviewer_user, reviewer_headers
):
    from tests.conftest import make_company

    other = make_company(db_session, "Other Co")
    res = client.patch(
        f"/settings/users/{reviewer_user.id}", headers=platform_admin_headers, json={"company_id": str(other.id)}
    )
    assert res.status_code == 200 and res.json()["company_name"] == "Other Co"
    # The old token still names the old company: it is refused, not silently re-scoped.
    assert client.get("/cases", headers=reviewer_headers).status_code == 401


def test_platform_admin_cannot_demote_or_deactivate_themselves(client, platform_admin_headers, platform_admin_user):
    uid = platform_admin_user.id
    assert client.patch(f"/settings/users/{uid}", headers=platform_admin_headers, json={"role": "user"}).status_code == 400
    assert client.patch(f"/settings/users/{uid}", headers=platform_admin_headers, json={"is_active": False}).status_code == 400
    assert client.patch(f"/settings/users/{uid}", headers=platform_admin_headers, json={"role": "platform_admin"}).status_code == 200  # no-op ok


def test_user_update_validation(client, platform_admin_headers):
    h = platform_admin_headers
    assert client.patch("/settings/users/00000000-0000-0000-0000-000000000000", headers=h, json={}).status_code == 422
    assert client.patch("/settings/users/00000000-0000-0000-0000-000000000000", headers=h, json={"role": "root"}).status_code == 422
    assert client.patch("/settings/users/00000000-0000-0000-0000-000000000000", headers=h, json={"role": "user"}).status_code == 404


def test_platform_admin_can_reset_password_and_is_audited(client, db_session, platform_admin_headers, plain_user, reviewer_headers):
    # Non-admin cannot reset password
    assert client.post(
        f"/settings/users/{plain_user.id}/reset-password",
        headers=reviewer_headers,
        json={"password": "NewValidPassword123!"},
    ).status_code == 403

    # Password too short returns 422
    assert client.post(
        f"/settings/users/{plain_user.id}/reset-password",
        headers=platform_admin_headers,
        json={"password": "short"},
    ).status_code == 422

    # Platform admin resets password successfully
    res = client.post(
        f"/settings/users/{plain_user.id}/reset-password",
        headers=platform_admin_headers,
        json={"password": "BrandNewPassword123!"},
    )
    assert res.status_code == 200
    assert res.json()["id"] == str(plain_user.id)

    # User can now log in with the new password
    login_res = client.post("/auth/login", json={"email": plain_user.email, "password": "BrandNewPassword123!"})
    assert login_res.status_code == 200

    # Old password no longer works
    login_old = client.post("/auth/login", json={"email": plain_user.email, "password": "TestPassword123!"})
    assert login_old.status_code == 401

    # Audit log entry exists
    events = _events(db_session, "user_password_reset")
    assert any(e.event_data.get("user_id") == str(plain_user.id) for e in events)

