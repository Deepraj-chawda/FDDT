import uuid

from app.models.cross_document_finding import CrossDocumentFinding, FindingSeverity
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from tests.sample_files import PDF


def _create_case(client, auth_headers, case_type="vendor_invoice"):
    return client.post(
        "/cases", json={"case_type": case_type}, headers=auth_headers
    ).json()


def _upload(client, auth_headers, case_id, filename="doc.pdf"):
    return client.post(
        f"/cases/{case_id}/documents",
        headers=auth_headers,
        files={"file": (filename, PDF, "application/pdf")},
    ).json()


def test_list_cases_requires_auth(client):
    assert client.get("/cases").status_code == 401


def test_get_case_requires_auth(client):
    response = client.get("/cases/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 401


def test_list_cases_includes_document_count_and_submitter(client, auth_headers, seeded_user):
    case = _create_case(client, auth_headers)
    _upload(client, auth_headers, case["id"], "a.pdf")
    _upload(client, auth_headers, case["id"], "b.pdf")

    response = client.get("/cases", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    listed = next(c for c in body if c["id"] == case["id"])
    assert listed["document_count"] == 2
    assert listed["submitted_by"]["email"] == seeded_user.email
    assert listed["case_type"] == "vendor_invoice"
    # Not scored yet (pipeline hasn't run in this test) -> "pending".
    assert listed["flag"]["flag"] == "pending"


def test_list_cases_with_no_documents_has_zero_count(client, auth_headers):
    case = _create_case(client, auth_headers)
    body = client.get("/cases", headers=auth_headers).json()
    listed = next(c for c in body if c["id"] == case["id"])
    assert listed["document_count"] == 0
    assert listed["flag"]["flag"] == "pending"


def test_list_cases_shows_real_tier_from_latest_assessment(client, auth_headers, db_session):
    """A scored case's flag is its assessment tier — including a
    single-document case (forensics ran on that one document)."""
    from datetime import datetime, timezone

    from app.models.case import RiskTier
    from app.models.case_risk_assessment import CaseRiskAssessment

    case = _create_case(client, auth_headers)
    _upload(client, auth_headers, case["id"], "a.pdf")
    db_session.add(
        CaseRiskAssessment(
            case_id=uuid.UUID(case["id"]),
            raw_score=72,
            score=72,
            tier=RiskTier.high,
            triggered_reasons=[{"reason": "Editing software found."}],
            risk_rules_version_snapshot={},
            evidence_fingerprint="fp",
            computed_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    body = client.get("/cases", headers=auth_headers).json()
    listed = next(c for c in body if c["id"] == case["id"])
    assert listed["flag"]["flag"] == "high"
    assert listed["flag"]["score"] == 72
    assert listed["assigned_tier"] == "l1" and listed["can_act"] is True


def test_list_cases_filter_by_status(client, auth_headers):
    case = _create_case(client, auth_headers)

    matching = client.get("/cases?status=submitted", headers=auth_headers).json()
    assert any(c["id"] == case["id"] for c in matching)

    non_matching = client.get("/cases?status=closed", headers=auth_headers).json()
    assert not any(c["id"] == case["id"] for c in non_matching)


def test_list_cases_filter_by_case_type(client, auth_headers):
    invoice_case = _create_case(client, auth_headers, "vendor_invoice")
    quotation_case = _create_case(client, auth_headers, "quotation")

    body = client.get("/cases?case_type=quotation", headers=auth_headers).json()
    ids = {c["id"] for c in body}
    assert quotation_case["id"] in ids
    assert invoice_case["id"] not in ids


def test_list_cases_invalid_filter_is_422(client, auth_headers):
    response = client.get("/cases?status=not-a-real-status", headers=auth_headers)
    assert response.status_code == 422


def test_get_case_detail_returns_documents(client, auth_headers):
    case = _create_case(client, auth_headers)
    uploaded = _upload(client, auth_headers, case["id"], "invoice.pdf")

    response = client.get(f"/cases/{case['id']}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == case["id"]
    assert body["document_count"] == 1
    assert len(body["documents"]) == 1

    doc = body["documents"][0]
    assert doc["id"] == uploaded["id"]
    assert doc["original_filename"] == "invoice.pdf"
    assert doc["document_type"] is None  # classification not built yet
    # Both the upload response and case detail carry a signed download URL
    # (the container is private), not the bare durable one stored in the DB.
    assert "?fake-sas-token" in uploaded["file_url"]
    assert "?fake-sas-token" in doc["file_url"]
    assert doc["file_url"].split("?")[0] == uploaded["file_url"].split("?")[0]
    assert doc["checks"] == []
    assert body["cross_document_findings"] == []
    assert body["flag"]["flag"] == "pending"
    assert body["assessment"] is None
    assert body["pipeline"]["complete"] is False
    assert body["actions"] == []


def test_get_case_detail_returns_document_checks_and_cross_document_findings(
    client, auth_headers, db_session
):
    """Document checks and cross-document findings are written by Celery
    tasks (app/tasks/document_checks.py), not this endpoint — this test
    inserts rows directly (as those tasks would) to verify the case-detail
    response actually surfaces them."""
    case = _create_case(client, auth_headers)
    uploaded = _upload(client, auth_headers, case["id"], "invoice.pdf")

    db_session.add(
        DocumentCheck(
            document_id=uuid.UUID(uploaded["id"]),
            check_type=DocumentCheckType.field_validation,
            status=DocumentCheckStatus.completed,
            result={"result": "flag", "details": {"date_in_future": {"status": "flag", "reason": "..."}}},
        )
    )
    db_session.add(
        CrossDocumentFinding(
            case_id=uuid.UUID(case["id"]),
            field_name="amount",
            finding_type="cross_document_consistency",
            severity=FindingSeverity.medium,
            description="Amount differs between 'invoice.pdf' and 'receipt.pdf'.",
            document_ids=[uploaded["id"]],
        )
    )
    db_session.commit()

    response = client.get(f"/cases/{case['id']}", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()

    doc = body["documents"][0]
    assert len(doc["checks"]) == 1
    assert doc["checks"][0]["check_type"] == "field_validation"
    assert doc["checks"][0]["result"]["result"] == "flag"

    assert len(body["cross_document_findings"]) == 1
    assert body["cross_document_findings"][0]["field_name"] == "amount"
    assert body["cross_document_findings"][0]["severity"] == "medium"


def test_get_case_detail_unknown_case_404(client, auth_headers):
    response = client.get(
        "/cases/00000000-0000-0000-0000-000000000000", headers=auth_headers
    )
    assert response.status_code == 404


def test_get_case_detail_with_no_documents(client, auth_headers):
    case = _create_case(client, auth_headers)
    response = client.get(f"/cases/{case['id']}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["documents"] == []
