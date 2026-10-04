"""
Multi-tenancy, application layer: company isolation, platform-admin support
access (audited), per-company settings, billing counters.

Runs on SQLite, so it exercises layer 1 (explicit company filters + the
session guards in app/db/tenancy.py). Layer 2 — PostgreSQL Row-Level Security
on its own, with application filters deliberately omitted — is
tests/test_rls_postgres.py.
"""
import uuid

import pytest
from sqlalchemy import select

from app.db import tenancy
from app.models.audit_log import AuditLog
from app.models.case import Case, CaseStatus, CaseType
from app.models.company_usage_stats import CompanyUsageStats
from app.models.document import Document
from app.models.issuer_registry import IssuerRegistry
from app.models.risk_rule import RiskRule
from app.models.user import UserRole
from app.services.forensics.duplicate_check import run_duplicate_check
from app.services.risk_rule_seed import SEED_RULES
from app.services.storage_service import CrossTenantBlobError, ensure_company_blob
from tests.conftest import headers_for, make_user
from tests.sample_files import PDF, make_pdf


# ---------------------------------------------------------------------------
# Fixtures: two companies (A, B) created by a platform admin through the API
# ---------------------------------------------------------------------------


@pytest.fixture()
def tenants(client, db_session, platform_admin_headers):
    """Test 1: a platform admin creates Company A and Company B and their users."""
    out = {}
    for key, name in (("a", "Company A"), ("b", "Company B")):
        res = client.post("/platform/companies", headers=platform_admin_headers, json={"name": name})
        assert res.status_code == 201, res.text
        company_id = res.json()["id"]
        users = {}
        for role in ("user", "reviewer_l1", "reviewer_l2"):
            created = client.post(
                "/settings/users",
                headers=platform_admin_headers,
                json={
                    "email": f"{role}.{key}@example.com",
                    "full_name": f"{role} {name}",
                    "role": role,
                    "password": "S3cure-pass!",
                    "company_id": company_id,
                },
            )
            assert created.status_code == 201, created.text
            login = client.post("/auth/login", json={"email": f"{role}.{key}@example.com", "password": "S3cure-pass!"})
            assert login.status_code == 200
            users[role] = {"Authorization": f"Bearer {login.json()['access_token']}"}
        out[key] = {"id": company_id, "name": name, "headers": users}
    return out


def _case_with_document(client, headers, filename="invoice.pdf", content=PDF):
    case = client.post("/cases", headers=headers, json={"case_type": "vendor_invoice"})
    assert case.status_code == 201, case.text
    case_id = case.json()["id"]
    doc = client.post(
        f"/cases/{case_id}/documents",
        headers=headers,
        files={"file": (filename, content, "application/pdf")},
    )
    assert doc.status_code == 201, doc.text
    return case_id, doc.json()["id"]


# ---------------------------------------------------------------------------
# Company creation seeds its own settings
# ---------------------------------------------------------------------------


def test_new_company_gets_its_own_default_rule_set(client, tenants):
    a_rules = client.get("/settings/risk-rules", headers=tenants["a"]["headers"]["reviewer_l2"]).json()
    b_rules = client.get("/settings/risk-rules", headers=tenants["b"]["headers"]["reviewer_l2"]).json()
    assert len(a_rules) == len(b_rules) == len(SEED_RULES)
    assert {r["id"] for r in a_rules}.isdisjoint({r["id"] for r in b_rules})  # independent rows

    # Tuning A's copy leaves B's untouched.
    rule_id = a_rules[0]["rule_id"]
    res = client.patch(
        f"/settings/risk-rules/{rule_id}",
        headers=tenants["a"]["headers"]["reviewer_l2"],
        json={"weight": 99, "change_note": "A only"},
    )
    assert res.status_code == 200 and res.json()["version"] == 2
    b_after = {r["rule_id"]: r for r in client.get("/settings/risk-rules", headers=tenants["b"]["headers"]["reviewer_l2"]).json()}
    assert b_after[rule_id]["version"] == 1 and b_after[rule_id]["weight"] != 99

    thresholds = client.get("/settings/risk-thresholds", headers=tenants["b"]["headers"]["reviewer_l2"])
    assert thresholds.status_code == 200 and thresholds.json()["medium_threshold"] == 30


# ---------------------------------------------------------------------------
# Test 2: Company A's reviewer_l2 sees A and gets 404 on anything of B by id
# ---------------------------------------------------------------------------


def test_reviewer_l2_sees_own_company_and_gets_404_for_the_other(client, tenants, fake_storage):
    a, b = tenants["a"], tenants["b"]
    a_l2 = a["headers"]["reviewer_l2"]
    a_case, a_doc = _case_with_document(client, a["headers"]["user"], "a-invoice.pdf")
    b_case, b_doc = _case_with_document(client, b["headers"]["user"], "b-invoice.pdf")

    # Own company: visible.
    listed = client.get("/cases", headers=a_l2)
    assert listed.status_code == 200 and [c["id"] for c in listed.json()] == [a_case]
    assert client.get(f"/cases/{a_case}", headers=a_l2).status_code == 200

    issuer = client.post(
        "/settings/issuers", headers=b["headers"]["reviewer_l2"], json={"name": "B's Secret Vendor", "type": "vendor"}
    )
    assert issuer.status_code == 201
    b_issuer_id = issuer.json()["id"]
    assert client.get("/settings/issuers", headers=a_l2).json() == []  # B's issuer not in A's registry
    assert client.get("/settings/risk-rules", headers=a_l2).status_code == 200

    # Company B's resources, addressed directly by id: 404, never an empty 200.
    for method, path, body in [
        ("get", f"/cases/{b_case}", None),
        ("get", f"/cases/{b_case}/audit-log", None),
        ("get", f"/cases/{b_case}/documents/{b_doc}/file-url", None),
        ("get", f"/cases/{b_case}/signature-references", None),
        ("get", f"/cases/{b_case}/signature-matches", None),
        ("get", f"/cases/{b_case}/reports", None),
        ("post", f"/cases/{b_case}/reports", None),
        ("post", f"/cases/{b_case}/approve", {"note": "x" * 20}),
        ("post", f"/cases/{b_case}/reject", {"reason": "not mine"}),
        ("post", f"/cases/{b_case}/escalate", {"reason": "not mine"}),
        ("patch", f"/settings/issuers/{b_issuer_id}", {"name": "hijacked"}),
        ("get", f"/cases?company_id={b['id']}", None),
        ("get", f"/settings/issuers?company_id={b['id']}", None),
        ("get", f"/settings/risk-rules?company_id={b['id']}", None),
    ]:
        kwargs = {"headers": a_l2, **({"json": body} if body is not None else {})}
        res = getattr(client, method)(path, **kwargs)
        assert res.status_code == 404, f"{method.upper()} {path} -> {res.status_code} {res.text}"

    # A's document in a B case id (mixed ids) is also refused.
    assert client.get(f"/cases/{b_case}/documents/{a_doc}/file-url", headers=a_l2).status_code == 404
    # Uploading into B's case is refused too.
    upload = client.post(
        f"/cases/{b_case}/documents", headers=a_l2, files={"file": ("x.pdf", PDF, "application/pdf")}
    )
    assert upload.status_code == 404


def test_a_rule_history_of_another_company_is_a_404(client, tenants):
    b_l2 = tenants["b"]["headers"]["reviewer_l2"]
    created = client.post(
        "/settings/risk-rules",
        headers=b_l2,
        json={
            "rule_id": "b.only_rule", "category": "forensics", "match": "check_result",
            "check_type": "metadata_forensics", "weight": 5, "severity": "low", "reason_template": "Because.",
        },
    )
    assert created.status_code == 201, created.text
    a_l2 = tenants["a"]["headers"]["reviewer_l2"]
    assert client.get("/settings/risk-rules/b.only_rule/history", headers=a_l2).status_code == 404
    assert client.patch("/settings/risk-rules/b.only_rule", headers=a_l2, json={"weight": 1}).status_code == 404
    # ...and A may create a rule with the same id — rule ids are per company.
    same = client.post(
        "/settings/risk-rules",
        headers=a_l2,
        json={
            "rule_id": "b.only_rule", "category": "forensics", "match": "check_result",
            "check_type": "metadata_forensics", "weight": 7, "severity": "low", "reason_template": "A's.",
        },
    )
    assert same.status_code == 201


# ---------------------------------------------------------------------------
# Test 3: Settings > Users is unreachable for a company reviewer_l2
# ---------------------------------------------------------------------------


def test_reviewer_l2_cannot_reach_settings_users(client, tenants):
    a_l2 = tenants["a"]["headers"]["reviewer_l2"]
    assert client.get("/settings/users", headers=a_l2).status_code == 403
    assert client.post(
        "/settings/users", headers=a_l2,
        json={"email": "sneaky@example.com", "password": "S3cure-pass!", "company_id": tenants["a"]["id"]},
    ).status_code == 403
    # Nor any platform screen.
    for path in ("/platform/companies", "/platform/usage", "/platform/queues"):
        assert client.get(path, headers=a_l2).status_code == 403


def test_reviewer_l1_cannot_reach_company_settings(client, tenants):
    a_l1 = tenants["a"]["headers"]["reviewer_l1"]
    assert client.get("/settings/issuers", headers=a_l1).status_code == 403
    assert client.get("/settings/risk-rules", headers=a_l1).status_code == 403


# ---------------------------------------------------------------------------
# Test 4: platform admin opens Company B's case — allowed and audited
# ---------------------------------------------------------------------------


def _platform_access_rows(db_session):
    state = tenancy.snapshot(db_session)
    tenancy.bind_platform(db_session)
    try:
        return db_session.execute(
            select(AuditLog).where(AuditLog.event_type == "platform_admin_access").order_by(AuditLog.created_at)
        ).scalars().all()
    finally:
        tenancy.restore(db_session, state)


def test_platform_admin_reads_any_case_and_each_access_is_audited(
    client, db_session, tenants, platform_admin_headers, platform_admin_user
):
    b = tenants["b"]
    b_case, b_doc = _case_with_document(client, b["headers"]["user"], "b-invoice.pdf")

    res = client.get(f"/cases/{b_case}", headers=platform_admin_headers)
    assert res.status_code == 200 and res.json()["id"] == b_case

    rows = _platform_access_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    case_number = res.json()["case_number"]
    # Platform-only: no company owns the row (RLS hides it from every company
    # session); the target company is recorded in the event data.
    assert row.company_id is None
    assert row.event_data["company_id"] == b["id"] and str(row.case_id) == b_case
    assert row.actor_user_id == platform_admin_user.id
    assert row.event_data["summary"] == f"Platform admin {platform_admin_user.email} viewed Company B / Case {case_number}"

    # Every access is recorded individually — no de-duplication window.
    client.get(f"/cases/{b_case}", headers=platform_admin_headers)
    client.get(f"/cases/{b_case}", headers=platform_admin_headers)
    client.get(f"/cases/{b_case}/documents/{b_doc}/file-url", headers=platform_admin_headers)
    paths = [r.event_data["path"] for r in _platform_access_rows(db_session)]
    assert paths == [f"/cases/{b_case}"] * 3 + [f"/cases/{b_case}/documents/{b_doc}/file-url"]

    # The platform admin sees all of them: in the platform-level log and in
    # Company B's log view.
    platform_log = client.get("/audit-log?event_type=platform_admin_access", headers=platform_admin_headers).json()
    assert platform_log["total"] == 4
    b_view = client.get(
        f"/audit-log?event_type=platform_admin_access&company_id={b['id']}", headers=platform_admin_headers
    ).json()
    assert b_view["total"] == 5  # the 4 above + this audit-log read itself (also audited)


def test_company_users_never_see_platform_admin_access(client, db_session, tenants, platform_admin_headers):
    """Not in Audit History, not in the event-type filter, not in the case
    timeline, not in a generated report — for any company role."""
    b = tenants["b"]
    b_case, _ = _case_with_document(client, b["headers"]["user"], "b-invoice.pdf")
    for _ in range(3):
        assert client.get(f"/cases/{b_case}", headers=platform_admin_headers).status_code == 200
    client.get(f"/cases/{b_case}/audit-log", headers=platform_admin_headers)
    assert len(_platform_access_rows(db_session)) == 4

    for role in ("reviewer_l1", "reviewer_l2"):
        h = b["headers"][role]
        log = client.get("/audit-log?limit=500", headers=h).json()
        assert all(item["event_type"] != "platform_admin_access" for item in log["items"]), role
        assert client.get("/audit-log?event_type=platform_admin_access", headers=h).json()["total"] == 0
        assert "platform_admin_access" not in client.get("/audit-log/event-types", headers=h).json()
        timeline = client.get(f"/cases/{b_case}/audit-log", headers=h).json()
        assert all(e["event_type"] != "platform_admin_access" for e in timeline), role
    owner_timeline = client.get(f"/cases/{b_case}/audit-log", headers=b["headers"]["user"]).json()
    assert all(e["event_type"] != "platform_admin_access" for e in owner_timeline)
    # Company A, too.
    assert client.get("/audit-log?event_type=platform_admin_access", headers=tenants["a"]["headers"]["reviewer_l2"]).json()["total"] == 0


def test_company_log_names_platform_actions_platform_team(client, tenants):
    """The `tenants` fixture's users were created by a platform admin, whose
    user row a company session cannot see. The company's log must not show
    those events as automated ("System pipeline") nor reveal the admin."""
    log = client.get("/audit-log?event_type=user_created", headers=tenants["a"]["headers"]["reviewer_l2"]).json()
    assert log["total"] >= 1
    assert all(item["actor_name"] == "Platform team" for item in log["items"])
    assert all(item["actor_email"] is None for item in log["items"])


def test_platform_admin_access_is_read_only(client, tenants, platform_admin_headers):
    b = tenants["b"]
    b_case, _ = _case_with_document(client, b["headers"]["user"])
    h = platform_admin_headers
    assert client.post("/cases", headers=h, json={"case_type": "vendor_invoice"}).status_code == 403
    assert client.post(f"/cases/{b_case}/approve", headers=h, json={"note": "x" * 20}).status_code == 403
    assert client.post(f"/cases/{b_case}/escalate", headers=h, json={"reason": "x"}).status_code == 403
    assert client.post(f"/cases/{b_case}/reports", headers=h).status_code == 403
    assert client.post(
        f"/cases/{b_case}/documents", headers=h, files={"file": ("x.pdf", PDF, "application/pdf")}
    ).status_code == 403


def test_platform_admin_case_list_is_one_company_at_a_time(client, tenants, platform_admin_headers):
    a_case, _ = _case_with_document(client, tenants["a"]["headers"]["user"])
    b_case, _ = _case_with_document(client, tenants["b"]["headers"]["user"])
    assert client.get("/cases", headers=platform_admin_headers).status_code == 400  # no mixed list
    only_a = client.get(f"/cases?company_id={tenants['a']['id']}", headers=platform_admin_headers)
    assert only_a.status_code == 200 and [c["id"] for c in only_a.json()] == [a_case]
    assert client.get(f"/cases?company_id={uuid.uuid4()}", headers=platform_admin_headers).status_code == 404


def test_platform_admin_manages_a_companys_settings_with_audit(client, db_session, tenants, platform_admin_headers):
    b = tenants["b"]
    res = client.post(
        f"/settings/issuers?company_id={b['id']}", headers=platform_admin_headers,
        json={"name": "Support-added Vendor", "type": "vendor"},
    )
    assert res.status_code == 201
    assert [i["name"] for i in client.get("/settings/issuers", headers=b["headers"]["reviewer_l2"]).json()] == [
        "Support-added Vendor"
    ]
    assert client.get("/settings/issuers", headers=platform_admin_headers).status_code == 400  # must pick a company
    # The change is audited (platform-only).
    audit = client.get(
        f"/audit-log?event_type=platform_admin_access&company_id={b['id']}", headers=platform_admin_headers
    ).json()
    assert any(item["event_data"]["method"] == "POST" for item in audit["items"])


# ---------------------------------------------------------------------------
# Test 5: billing dashboard — per company, matching the actual uploads
# ---------------------------------------------------------------------------


def test_billing_dashboard_separates_companies_and_matches_uploads(
    client, db_session, tenants, platform_admin_headers, fake_storage
):
    a, b = tenants["a"], tenants["b"]
    files_a = [make_pdf(f"A{i}", filler=n) for i, n in enumerate((0, 1500, 40000))]
    files_b = [make_pdf("B0", filler=700), make_pdf("B1")]
    sizes_a = [len(f) for f in files_a]
    sizes_b = [len(f) for f in files_b]
    assert len(set(sizes_a)) == 3  # distinct sizes, so the sums prove per-file accounting
    for i, content in enumerate(files_a):
        _case_with_document(client, a["headers"]["user"], f"a{i}.pdf", content)
    case_b, _ = _case_with_document(client, b["headers"]["user"], "b0.pdf", files_b[0])
    # A second document in B's case.
    client.post(f"/cases/{case_b}/documents", headers=b["headers"]["user"],
                files={"file": ("b1.pdf", files_b[1], "application/pdf")})

    for period in ("this_month", "all_time"):
        usage = client.get(f"/platform/usage?period={period}", headers=platform_admin_headers)
        assert usage.status_code == 200, usage.text
        rows = {r["company_name"]: r for r in usage.json()["companies"]}
        assert rows["Company A"]["cases_created"] == 3
        assert rows["Company A"]["documents_uploaded"] == 3
        assert rows["Company A"]["storage_bytes"] == sum(sizes_a)
        assert rows["Company B"]["cases_created"] == 1
        assert rows["Company B"]["documents_uploaded"] == 2
        assert rows["Company B"]["storage_bytes"] == sum(sizes_b)
        assert rows["Company B"]["files_stored"] == 2

    # Manual cross-check against the source rows, for Company A.
    tenancy.bind_platform(db_session)
    try:
        docs = db_session.execute(
            select(Document).where(Document.company_id == uuid.UUID(a["id"]))
        ).scalars().all()
        assert len(docs) == 3 and sum(d.file_size_bytes for d in docs) == sum(sizes_a)
        # Every A file is under A's own blob prefix.
        assert all(f"/companies/{a['id']}/cases/" in d.blob_storage_path for d in docs)
    finally:
        tenancy.bind_company(db_session, db_session.info["test_company_id"])

    # Nothing to correct after normal operation.
    first = client.post("/platform/usage/reconcile", headers=platform_admin_headers).json()
    assert first["rows_corrected"] == 0

    # Simulate drift (a lost increment) — reconciliation repairs it.
    tenancy.bind_platform(db_session)
    try:
        row = db_session.execute(
            select(CompanyUsageStats).where(CompanyUsageStats.company_id == uuid.UUID(a["id"]))
        ).scalar_one()
        row.documents_uploaded = 99
        row.storage_bytes = 1
        db_session.commit()
    finally:
        tenancy.bind_company(db_session, db_session.info["test_company_id"])
    fixed = client.post("/platform/usage/reconcile", headers=platform_admin_headers).json()
    assert fixed["rows_corrected"] == 1
    assert fixed["drift"][0]["counters"]["documents_uploaded"] == {"was": 99, "now": 3}
    rows = {r["company_name"]: r for r in client.get("/platform/usage?period=all_time", headers=platform_admin_headers).json()["companies"]}
    assert rows["Company A"]["documents_uploaded"] == 3 and rows["Company A"]["storage_bytes"] == sum(sizes_a)


def test_usage_custom_period_and_validation(client, tenants, platform_admin_headers):
    h = platform_admin_headers
    assert client.get("/platform/usage?period=custom", headers=h).status_code == 422
    assert client.get("/platform/usage?period=custom&start=2026-02-01&end=2026-01-01", headers=h).status_code == 422
    past = client.get("/platform/usage?period=custom&start=2000-01-01&end=2000-01-31", headers=h).json()
    assert all(r["documents_uploaded"] == 0 for r in past["companies"])


# ---------------------------------------------------------------------------
# Companies: suspension, uniqueness
# ---------------------------------------------------------------------------


def test_suspended_company_users_are_locked_out(client, tenants, platform_admin_headers):
    a = tenants["a"]
    assert client.get("/cases", headers=a["headers"]["reviewer_l1"]).status_code == 200
    res = client.patch(f"/platform/companies/{a['id']}", headers=platform_admin_headers, json={"is_active": False})
    assert res.status_code == 200 and res.json()["is_active"] is False
    assert client.get("/cases", headers=a["headers"]["reviewer_l1"]).status_code == 401
    assert client.post("/auth/login", json={"email": "reviewer_l1.a@example.com", "password": "S3cure-pass!"}).status_code == 401
    # Company B is unaffected.
    assert client.get("/cases", headers=tenants["b"]["headers"]["reviewer_l1"]).status_code == 200


def test_company_names_are_unique(client, tenants, platform_admin_headers):
    assert client.post("/platform/companies", headers=platform_admin_headers, json={"name": "company a"}).status_code == 409


# ---------------------------------------------------------------------------
# Session guards (app/db/tenancy.py)
# ---------------------------------------------------------------------------


def test_company_session_never_returns_another_companys_rows_even_without_a_filter(db_session, tenants):
    a_id, b_id = uuid.UUID(tenants["a"]["id"]), uuid.UUID(tenants["b"]["id"])
    tenancy.bind_platform(db_session)
    rule_a = db_session.execute(select(RiskRule).where(RiskRule.company_id == a_id)).scalars().first()
    assert rule_a is not None
    rule_a_id = rule_a.id
    tenancy.bind_company(db_session, b_id)
    try:
        # Deliberately unfiltered queries: still only B's rows.
        companies_seen = {r.company_id for r in db_session.execute(select(RiskRule)).scalars().all()}
        assert companies_seen == {b_id}
        # Even a primary-key get of A's row (cached in the identity map from
        # the platform-mode read above) is not served to a B session.
        assert db_session.get(RiskRule, rule_a_id) is None
    finally:
        tenancy.bind_company(db_session, db_session.info["test_company_id"])


def test_unbound_session_refuses_tenant_tables(db_session):
    tenancy.restore(db_session, (None, None))
    try:
        with pytest.raises(tenancy.TenantContextError):
            db_session.execute(select(Case)).all()
    finally:
        tenancy.bind_company(db_session, db_session.info["test_company_id"])


def test_company_session_refuses_to_write_another_companys_row(db_session, tenants, seeded_user):
    db_session.add(
        IssuerRegistry(company_id=uuid.UUID(tenants["b"]["id"]), name="Smuggled", is_active=True)
    )
    with pytest.raises(tenancy.TenantContextError):
        db_session.flush()
    db_session.rollback()


def test_new_rows_are_stamped_with_the_session_company(db_session, seeded_user):
    case = Case(case_number="CASE-STAMP001", case_type=CaseType.other, submitted_by_user_id=seeded_user.id,
                status=CaseStatus.submitted)
    db_session.add(case)
    db_session.commit()
    assert case.company_id == db_session.info["test_company_id"]


# ---------------------------------------------------------------------------
# Storage + duplicate detection stay inside the company
# ---------------------------------------------------------------------------


def test_storage_refuses_to_sign_another_companys_blob():
    a, b = uuid.uuid4(), uuid.uuid4()
    url = f"https://acct.blob.core.windows.net/documents/companies/{a}/cases/{uuid.uuid4()}/documents/x.pdf"
    ensure_company_blob(url, a)
    with pytest.raises(CrossTenantBlobError):
        ensure_company_blob(url, b)
    # Pre-multi-tenancy paths carry no company and stay readable (their rows are scoped).
    ensure_company_blob(f"https://acct.blob.core.windows.net/documents/{uuid.uuid4()}/legacy.pdf", b)


def test_duplicate_detection_never_matches_across_companies(db_session, tenants, sample_document_pages):
    page = next(iter(sample_document_pages.values()))[:1]
    a_id, b_id = uuid.UUID(tenants["a"]["id"]), uuid.UUID(tenants["b"]["id"])

    def _doc(company_id):
        tenancy.bind_platform(db_session)
        user = make_user(db_session, f"dup.{uuid.uuid4().hex[:6]}@example.com", UserRole.user, company_id=company_id)
        tenancy.bind_company(db_session, company_id)
        case = Case(case_number=f"CASE-{uuid.uuid4().hex[:8].upper()}", case_type=CaseType.other,
                    submitted_by_user_id=user.id, status=CaseStatus.submitted)
        db_session.add(case)
        db_session.flush()
        doc = Document(case_id=case.id, uploaded_by_user_id=user.id, original_filename="same.pdf",
                       blob_storage_path="x", file_hash=uuid.uuid4().hex, content_type="application/pdf")
        db_session.add(doc)
        db_session.commit()
        return doc

    try:
        doc_a = _doc(a_id)
        tenancy.bind_company(db_session, a_id)
        run_duplicate_check(db_session, company_id=a_id, document_id=doc_a.id, pages=page)
        db_session.commit()

        doc_b = _doc(b_id)  # the identical page, in another company
        tenancy.bind_company(db_session, b_id)
        result = run_duplicate_check(db_session, company_id=b_id, document_id=doc_b.id, pages=page)
        assert result["result"] == "pass" and result["details"] == []

        doc_b2 = _doc(b_id)  # ...but a resubmission inside B is still caught
        tenancy.bind_company(db_session, b_id)
        again = run_duplicate_check(db_session, company_id=b_id, document_id=doc_b2.id, pages=page)
        assert again["result"] == "flag"
    finally:
        tenancy.bind_company(db_session, db_session.info["test_company_id"])
