"""Settings > Users > Add user, and Settings > Risk Rules > Add rule."""
import pytest
from sqlalchemy import select

from app.core.security import verify_password
from app.models.audit_log import AuditLog
from app.models.document_check import DocumentCheckType as T
from app.models.risk_rule import RiskRule
from app.models.user import User, UserRole
from app.services.risk_rule_seed import seed_risk_rules
from app.services.risk_scoring_service import load_current_rules, score_case
from tests.helpers_risk import add_document, finding, make_case

NEW_ENDPOINTS = [
    ("post", "/settings/users", {"email": "new.person@example.com", "password": "LongEnough1!"}),
    ("get", "/settings/risk-rule-options", None),
    ("post", "/settings/risk-rules", {
        "rule_id": "custom.x", "category": "forensics", "match": "check_result",
        "check_type": "metadata_forensics", "weight": 5, "severity": "low", "reason_template": "Because.",
    }),
]


@pytest.mark.parametrize("method,path,body", NEW_ENDPOINTS)
def test_new_endpoints_are_admin_only(client, reviewer_headers, plain_headers, method, path, body):
    # Users: platform admin only. Risk rules: Reviewer L2 (own company) or platform admin.
    assert getattr(client, method)(path, **({"json": body} if body is not None else {})).status_code == 401
    for headers in (reviewer_headers, plain_headers):
        kwargs = {"headers": headers, **({"json": body} if body is not None else {})}
        assert getattr(client, method)(path, **kwargs).status_code == 403


# ------------------------------------------------------------------- users

def test_admin_creates_a_user_who_can_log_in(client, platform_admin_headers, db_session, company):
    body = {
        "email": "New.Reviewer@Example.com", "full_name": "  Rina Reviewer ", "role": "reviewer_l1",
        "password": "S3cure-pass!", "company_id": str(company.id),
    }
    created = client.post("/settings/users", headers=platform_admin_headers, json=body)
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["role"] == "reviewer_l1" and data["is_active"] is True and data["full_name"] == "Rina Reviewer"
    assert data["company_id"] == str(company.id) and data["company_name"] == company.name
    assert "password" not in data and "hashed_password" not in data

    stored = db_session.execute(select(User).where(User.email == data["email"])).scalar_one()
    assert stored.hashed_password != "S3cure-pass!" and verify_password("S3cure-pass!", stored.hashed_password)

    login = client.post("/auth/login", json={"email": data["email"], "password": "S3cure-pass!"})
    assert login.status_code == 200 and login.json()["access_token"]
    assert any(u["email"] == data["email"] for u in client.get("/settings/users", headers=platform_admin_headers).json())

    event = db_session.execute(select(AuditLog).where(AuditLog.event_type == "user_created")).scalars().one()
    assert event.event_data["email"] == data["email"] and event.event_data["role"] == "reviewer_l1"
    assert "S3cure-pass!" not in str(event.event_data)  # the password is never logged


def test_role_defaults_to_user(client, platform_admin_headers, company):
    created = client.post(
        "/settings/users", headers=platform_admin_headers,
        json={"email": "plain@example.com", "password": "S3cure-pass!", "company_id": str(company.id)},
    )
    assert created.status_code == 201 and created.json()["role"] == "user"


def test_company_roles_need_a_company_and_platform_admins_have_none(client, platform_admin_headers, company):
    no_company = client.post(
        "/settings/users", headers=platform_admin_headers,
        json={"email": "orphan@example.com", "password": "S3cure-pass!", "role": "reviewer_l1"},
    )
    assert no_company.status_code == 422
    unknown = client.post(
        "/settings/users", headers=platform_admin_headers,
        json={"email": "ghost@example.com", "password": "S3cure-pass!", "company_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert unknown.status_code == 404
    admin = client.post(
        "/settings/users", headers=platform_admin_headers,
        json={"email": "ops@example.com", "password": "S3cure-pass!", "role": "platform_admin", "company_id": str(company.id)},
    )
    assert admin.status_code == 201 and admin.json()["company_id"] is None  # platform admins sit outside companies


def test_duplicate_email_is_rejected_ignoring_case(client, platform_admin_headers, seeded_user, company):
    dup = client.post("/settings/users", headers=platform_admin_headers,
                      json={"email": seeded_user.email.upper(), "password": "S3cure-pass!", "company_id": str(company.id)})
    assert dup.status_code == 409


@pytest.mark.parametrize("body", [
    {"email": "not-an-email", "password": "S3cure-pass!"},
    {"email": "short@example.com", "password": "short"},
    {"email": "role@example.com", "password": "S3cure-pass!", "role": "superuser"},
])
def test_invalid_user_payloads_are_422(client, platform_admin_headers, body):
    assert client.post("/settings/users", headers=platform_admin_headers, json=body).status_code == 422


# -------------------------------------------------------------------- rules

@pytest.fixture()
def rules(db_session):
    seed_risk_rules(db_session, db_session.info["test_company_id"])
    db_session.commit()


def _rule(**overrides):
    body = {
        "rule_id": "custom.hidden_layers", "category": "forensics", "match": "finding",
        "check_type": "metadata_forensics", "finding": "optional_content_groups", "severity_in": ["medium"],
        "weight": 12, "severity": "medium", "reason_template": "'{document}' has hidden layers. {description}",
    }
    body.update(overrides)
    return body


def test_options_describe_what_a_rule_can_be_built_from(client, auth_headers):
    options = client.get("/settings/risk-rule-options", headers=auth_headers).json()
    assert {m["value"] for m in options["match_kinds"]} == {
        "finding", "check_result", "sub_check", "cross_document", "signature_match"}
    metadata = next(c for c in options["finding_checks"] if c["value"] == "metadata_forensics")
    assert "editing_software_detected" in metadata["findings"]
    assert "consistent" not in {r["value"] for r in options["signature_results"]}
    assert "document" in options["placeholders"]


def test_create_rule_starts_at_version_1_and_is_listed_and_audited(client, auth_headers, db_session, rules, seeded_user):
    created = client.post("/settings/risk-rules", headers=auth_headers, json=_rule())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["rule_id"] == "custom.hidden_layers" and body["version"] == 1
    assert body["check_type"] == "metadata_forensics" and body["updated_by_name"] == seeded_user.full_name

    row = db_session.execute(select(RiskRule).where(RiskRule.rule_id == "custom.hidden_layers")).scalar_one()
    assert row.condition == {"match": "finding", "check_type": "metadata_forensics",
                             "finding": "optional_content_groups", "severity_in": ["medium"]}
    assert any(r["rule_id"] == "custom.hidden_layers" for r in client.get("/settings/risk-rules", headers=auth_headers).json())
    assert client.get("/settings/risk-rules/custom.hidden_layers/history", headers=auth_headers).json()[0]["version"] == 1

    event = db_session.execute(select(AuditLog).where(AuditLog.event_type == "risk_rule_created")).scalars().one()
    assert event.event_data["rule_id"] == "custom.hidden_layers" and event.event_data["weight"] == 12


def test_a_created_rule_actually_fires_when_a_case_is_scored(client, auth_headers, db_session, rules, seeded_user):
    case = make_case(db_session, seeded_user)
    add_document(db_session, case, seeded_user, "layers.pdf", checks={T.metadata_forensics: {
        "result": "flag", "details": [finding("optional_content_groups", "medium", "Has 2 optional content groups.")]}})
    before = score_case(db_session, db_session.info["test_company_id"], case.id)
    assert "custom.hidden_layers" not in [r["rule_id"] for r in before.triggered_reasons]

    assert client.post("/settings/risk-rules", headers=auth_headers, json=_rule()).status_code == 201
    # A second case scored after the rule exists picks it up; the first keeps its frozen assessment.
    case2 = make_case(db_session, seeded_user)
    add_document(db_session, case2, seeded_user, "layers2.pdf", checks={T.metadata_forensics: {
        "result": "flag", "details": [finding("optional_content_groups", "medium", "Has 2 optional content groups.")]}})
    after = score_case(db_session, db_session.info["test_company_id"], case2.id)
    fired = next(r for r in after.triggered_reasons if r["rule_id"] == "custom.hidden_layers")
    assert fired["weight"] == 12 and "layers2.pdf" in fired["reason"] and "hidden layers" in fired["reason"]
    assert any(r.rule_id == "custom.hidden_layers" for r in load_current_rules(db_session, db_session.info["test_company_id"]))


def test_rule_ids_are_normalised_to_lower_case(client, auth_headers):
    created = client.post("/settings/risk-rules", headers=auth_headers, json=_rule(rule_id=" Custom.Hidden_Layers "))
    assert created.status_code == 201 and created.json()["rule_id"] == "custom.hidden_layers"


def test_duplicate_rule_id_is_409(client, auth_headers, rules):
    assert client.post("/settings/risk-rules", headers=auth_headers, json=_rule()).status_code == 201
    assert client.post("/settings/risk-rules", headers=auth_headers, json=_rule()).status_code == 409
    seeded = client.post("/settings/risk-rules", headers=auth_headers, json=_rule(rule_id="ela.tamper_region_detected"))
    assert seeded.status_code == 409  # can't shadow a built-in rule either


@pytest.mark.parametrize("overrides", [
    {"finding": "no_such_finding"},                      # a name the check can't produce -> rule would never fire
    {"finding": "copy_move_cluster"},                    # ...or one that belongs to a different check
    {"check_type": "not_a_check"},
    {"rule_id": "No Dots Or Spaces"},
    {"weight": 500},
    {"reason_template": " "},
    {"category": "made_up"},
    {"match": "sub_check", "sub_check": "nope"},
    {"match": "cross_document", "field_name": "colour"},
    {"match": "signature_match", "signature_result": "consistent"},  # the good outcome isn't a risk
])
def test_rules_the_engine_could_not_evaluate_are_rejected(client, auth_headers, overrides):
    assert client.post("/settings/risk-rules", headers=auth_headers, json=_rule(**overrides)).status_code == 422


@pytest.mark.parametrize("overrides, condition, check_type", [
    ({"match": "check_result", "check_type": "issuer_verification", "finding": None, "severity_in": None},
     {"match": "check_result", "check_type": "issuer_verification", "result": "flag"}, "issuer_verification"),
    ({"match": "sub_check", "sub_check": "total_tax_consistency", "check_type": None, "finding": None, "severity_in": None},
     {"match": "sub_check", "check_type": "field_validation", "sub_check": "total_tax_consistency"}, "field_validation"),
    ({"match": "cross_document", "field_name": "amount", "check_type": None, "finding": None, "severity_in": ["high"]},
     {"match": "cross_document", "field_name": "amount", "severity_in": ["high"]}, "cross_document_consistency"),
    ({"match": "signature_match", "signature_result": "inconsistent", "check_type": None, "finding": None, "severity_in": None},
     {"match": "signature_match", "result": "inconsistent"}, "signature_comparison"),
])
def test_every_rule_type_builds_the_condition_the_engine_expects(client, auth_headers, db_session, overrides, condition, check_type):
    created = client.post("/settings/risk-rules", headers=auth_headers, json=_rule(**overrides))
    assert created.status_code == 201, created.text
    row = db_session.execute(select(RiskRule).where(RiskRule.rule_id == "custom.hidden_layers")).scalar_one()
    assert row.condition == condition and row.check_type == check_type
