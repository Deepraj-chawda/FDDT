"""Authorization on the routes that work on a case's contents: document upload, signed
file URLs, and the three signature endpoints.

Rules under test:
  * upload / file-url: the case owner, or any reviewer/admin — anyone else gets a 404
    (never a 403, so case ids cannot be probed);
  * signature references + matches: the case owner (who sets a reference signature while uploading)
    or any reviewer/admin — a stranger gets a 404.
"""
import uuid

import pytest

from tests.sample_files import PDF

BOX = {"page": 1, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.15}


def _new_case(client, headers):
    return client.post("/cases", json={"case_type": "vendor_invoice"}, headers=headers).json()["id"]


def _upload(client, headers, case_id):
    return client.post(
        f"/cases/{case_id}/documents", headers=headers, files={"file": ("a.pdf", PDF, "application/pdf")}
    )


@pytest.fixture()
def owned_case(client, plain_headers):
    """A case submitted by `plain_user`, with one uploaded document."""
    case_id = _new_case(client, plain_headers)
    doc = _upload(client, plain_headers, case_id)
    assert doc.status_code == 201
    return case_id, doc.json()["id"]


# ----------------------------------------------------------------------------- timeline blinding

def test_submitter_timeline_hides_risk_score_and_check_results(client, db_session, owned_case, plain_headers, reviewer_headers):
    """A submitter must not learn which signals fired: their case timeline has
    no scoring event and no result details on automated checks. A reviewer of
    the same company sees everything."""
    from app.db import tenancy
    from app.models.case import Case
    from app.services.audit_service import record_event

    case_id, document_id = owned_case
    state = tenancy.snapshot(db_session)
    tenancy.bind_platform(db_session)
    try:
        case = db_session.get(Case, uuid.UUID(case_id))
        record_event(db_session, "tampering_checks_completed", case_id=case.id, document_id=uuid.UUID(document_id),
                     company_id=case.company_id,
                     event_data={"ela_result": "flag", "copy_move_result": "pass", "page_count": 1, "ela_finding_count": 2})
        record_event(db_session, "risk_assessment_completed", case_id=case.id, company_id=case.company_id,
                     event_data={"tier": "high", "score": 75, "raw_score": 75, "rules_fired": ["ela.region_flagged"]})
        db_session.commit()
    finally:
        tenancy.restore(db_session, state)

    owner = client.get(f"/cases/{case_id}/audit-log", headers=plain_headers).json()
    assert "risk_assessment_completed" not in {e["event_type"] for e in owner}
    tampering = next(e for e in owner if e["event_type"] == "tampering_checks_completed")
    assert tampering["event_data"] == {"page_count": 1}
    leaked = {"score", "tier", "raw_score", "rules_fired", "ela_result", "ela_finding_count"}
    assert all(not (leaked & set(e["event_data"] or {})) for e in owner)

    reviewer = client.get(f"/cases/{case_id}/audit-log", headers=reviewer_headers).json()
    scored = next(e for e in reviewer if e["event_type"] == "risk_assessment_completed")
    assert scored["event_data"]["score"] == 75
    assert next(e for e in reviewer if e["event_type"] == "tampering_checks_completed")["event_data"]["ela_result"] == "flag"


# ----------------------------------------------------------------------------- upload

def test_owner_can_upload_to_own_case(client, plain_headers):
    case_id = _new_case(client, plain_headers)
    assert _upload(client, plain_headers, case_id).status_code == 201


def test_user_cannot_upload_to_someone_elses_case(client, plain_headers, other_plain_headers):
    case_id = _new_case(client, plain_headers)
    res = _upload(client, other_plain_headers, case_id)
    assert res.status_code == 404
    assert res.json()["detail"] == "Case not found"  # indistinguishable from a missing case


def test_reviewer_can_upload_to_any_case(client, plain_headers, reviewer_headers):
    case_id = _new_case(client, plain_headers)
    assert _upload(client, reviewer_headers, case_id).status_code == 201


def test_upload_to_missing_case_is_404_and_unauthenticated_is_401(client, plain_headers):
    assert _upload(client, plain_headers, uuid.uuid4()).status_code == 404
    res = client.post(f"/cases/{uuid.uuid4()}/documents", files={"file": ("a.pdf", PDF, "application/pdf")})
    assert res.status_code == 401


def test_rejected_upload_stores_nothing(client, plain_headers, other_plain_headers, fake_storage):
    case_id = _new_case(client, plain_headers)
    before = len(fake_storage.uploads)
    assert _upload(client, other_plain_headers, case_id).status_code == 404
    assert len(fake_storage.uploads) == before  # blocked before any bytes were written


# --------------------------------------------------------------------------- file-url

def test_file_url_owner_and_reviewer_allowed_other_user_404(
    client, owned_case, plain_headers, other_plain_headers, reviewer_headers
):
    case_id, doc_id = owned_case
    url = f"/cases/{case_id}/documents/{doc_id}/file-url"
    assert client.get(url, headers=plain_headers).status_code == 200
    assert client.get(url, headers=reviewer_headers).status_code == 200
    res = client.get(url, headers=other_plain_headers)
    assert res.status_code == 404
    assert "file_url" not in res.json()


# ------------------------------------------------------------------------- signatures

def _create_ref(client, headers, case_id, doc_id):
    return client.post(
        f"/cases/{case_id}/documents/{doc_id}/signature-references",
        json={"person_name": "Alice Signer", "bounding_box": BOX, "is_library": False},
        headers=headers,
    )


def test_owner_sets_reference_signature_during_upload_flow(client, owned_case, plain_headers, monkeypatch):
    """The uploader (a plain `user`) creates and reads reference signatures for their own case."""
    from app.tasks import signature_comparison_task

    enqueued = []
    monkeypatch.setattr(signature_comparison_task.run_signature_comparison, "delay", lambda *args: enqueued.append(args[0]))
    case_id, doc_id = owned_case
    assert _create_ref(client, plain_headers, case_id, doc_id).status_code == 201
    assert client.get(f"/cases/{case_id}/signature-references", headers=plain_headers).status_code == 200
    assert client.get(f"/cases/{case_id}/signature-matches", headers=plain_headers).status_code == 200
    assert len(enqueued) == 1


def test_stranger_cannot_use_signature_endpoints_on_someone_elses_case(
    client, owned_case, other_plain_headers, monkeypatch
):
    from app.tasks import signature_comparison_task

    enqueued = []
    monkeypatch.setattr(signature_comparison_task.run_signature_comparison, "delay", lambda *args: enqueued.append(args[0]))
    case_id, doc_id = owned_case
    assert _create_ref(client, other_plain_headers, case_id, doc_id).status_code == 404
    assert client.get(f"/cases/{case_id}/signature-references", headers=other_plain_headers).status_code == 404
    assert client.get(f"/cases/{case_id}/signature-matches", headers=other_plain_headers).status_code == 404
    assert enqueued == []  # nothing created, nothing queued


def test_reviewer_and_admin_can_use_signature_endpoints_on_any_case(
    client, owned_case, reviewer_headers, auth_headers, monkeypatch
):
    from app.tasks import signature_comparison_task

    enqueued = []
    monkeypatch.setattr(signature_comparison_task.run_signature_comparison, "delay", lambda *args: enqueued.append(args[0]))
    case_id, doc_id = owned_case
    for headers in (reviewer_headers, auth_headers):  # auth_headers is the seeded admin
        assert _create_ref(client, headers, case_id, doc_id).status_code == 201
        assert client.get(f"/cases/{case_id}/signature-references", headers=headers).status_code == 200
        assert client.get(f"/cases/{case_id}/signature-matches", headers=headers).status_code == 200
    assert len(enqueued) == 2


def test_signature_endpoints_404_on_unknown_case_and_401_unauthenticated(client, plain_headers):
    missing = uuid.uuid4()
    assert client.get(f"/cases/{missing}/signature-references", headers=plain_headers).status_code == 404
    assert client.get(f"/cases/{missing}/signature-matches", headers=plain_headers).status_code == 404
    assert client.get(f"/cases/{missing}/signature-matches").status_code == 401
