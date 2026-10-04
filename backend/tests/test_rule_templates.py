"""
Platform risk-rule templates: every new company starts from a copy of the
template; editing the template later never changes an existing company.
"""
from app.services.risk_rule_seed import SEED_RULES


def _company(client, headers, name):
    res = client.post("/platform/companies", headers=headers, json={"name": name})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _rules(client, headers, company_id):
    res = client.get(f"/settings/risk-rules?company_id={company_id}", headers=headers)
    assert res.status_code == 200, res.text
    return {r["rule_id"]: r for r in res.json()}


def test_new_company_gets_the_template_and_later_edits_dont_touch_it(client, platform_admin_headers):
    h = platform_admin_headers
    templates = client.get("/platform/risk-rule-templates", headers=h)
    assert templates.status_code == 200
    template = {t["rule_id"]: t for t in templates.json()}
    assert len(template) == len(SEED_RULES)

    first = _company(client, h, "Alpha Trading")
    alpha = _rules(client, h, first)
    # Populated immediately, equal to the template at creation time.
    assert set(alpha) == set(template)
    some_rule = sorted(template)[0]
    assert alpha[some_rule]["weight"] == template[some_rule]["weight"]
    assert all(r["version"] == 1 for r in alpha.values())

    # Edit the template afterwards.
    res = client.patch(
        f"/platform/risk-rule-templates/{some_rule}", headers=h,
        json={"weight": 77, "severity": "high", "is_active": True},
    )
    assert res.status_code == 200 and res.json()["weight"] == 77

    # The existing company is unchanged (no retroactive change)...
    assert _rules(client, h, first)[some_rule]["weight"] == template[some_rule]["weight"]
    # ...a company created after the edit starts from the new value.
    second = _company(client, h, "Beta Logistics")
    assert _rules(client, h, second)[some_rule]["weight"] == 77


def test_inactive_template_rules_are_not_copied(client, platform_admin_headers):
    h = platform_admin_headers
    rule_id = sorted(t["rule_id"] for t in client.get("/platform/risk-rule-templates", headers=h).json())[1]
    client.patch(f"/platform/risk-rule-templates/{rule_id}", headers=h, json={"is_active": False})
    company = _company(client, h, "Gamma Supplies")
    assert rule_id not in _rules(client, h, company)


def test_add_template_rule_and_company_reviewer_l2_sees_populated_rules(client, platform_admin_headers, db_session):
    h = platform_admin_headers
    created = client.post(
        "/platform/risk-rule-templates", headers=h,
        json={
            "rule_id": "platform.custom_metadata", "category": "forensics", "match": "check_result",
            "check_type": "metadata_forensics", "weight": 9, "severity": "low", "reason_template": "Platform default.",
        },
    )
    assert created.status_code == 201, created.text
    assert client.post(
        "/platform/risk-rule-templates", headers=h,
        json={"rule_id": "platform.custom_metadata", "category": "forensics", "match": "check_result",
              "check_type": "metadata_forensics", "weight": 1, "severity": "low", "reason_template": "dup"},
    ).status_code == 409

    company = _company(client, h, "Delta Imports")
    res = client.post("/settings/users", headers=h, json={
        "email": "l2@delta.example.com", "password": "S3cure-pass!", "role": "reviewer_l2", "company_id": company,
    })
    assert res.status_code == 201
    token = client.post("/auth/login", json={"email": "l2@delta.example.com", "password": "S3cure-pass!"}).json()
    l2 = {"Authorization": f"Bearer {token['access_token']}"}
    rules = client.get("/settings/risk-rules", headers=l2).json()
    assert len(rules) == len(SEED_RULES) + 1
    assert "platform.custom_metadata" in {r["rule_id"] for r in rules}


def test_templates_are_platform_admin_only(client, auth_headers, reviewer_headers):
    for headers in (auth_headers, reviewer_headers):
        assert client.get("/platform/risk-rule-templates", headers=headers).status_code == 403
        assert client.patch("/platform/risk-rule-templates/x", headers=headers, json={"weight": 1}).status_code == 403
