import uuid
import pytest

from tests.sample_files import PDF
from app.models.signature_reference import SignatureReference
from app.models.signature_match import SignatureMatch, ComparisonScope, SignatureMatchResult
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType

def test_signature_endpoints_require_auth(client):
    fake_case_id = uuid.uuid4()
    fake_doc_id = uuid.uuid4()
    res = client.post(
        f"/cases/{fake_case_id}/documents/{fake_doc_id}/signature-references",
        json={
            "person_name": "John Doe",
            "bounding_box": {"page": 1, "x": 0.1, "y": 0.1, "width": 0.2, "height": 0.1},
            "is_library": False,
        }
    )
    assert res.status_code == 401

    res_list = client.get(f"/cases/{fake_case_id}/signature-references")
    assert res_list.status_code == 401

    res_matches = client.get(f"/cases/{fake_case_id}/signature-matches")
    assert res_matches.status_code == 401

def test_create_signature_reference_in_case_and_library(client, auth_headers, fake_storage, monkeypatch):
    # Mock task .delay so it doesn't fail on Celery broker
    enqueued_tasks = []
    from app.tasks import signature_comparison_task
    monkeypatch.setattr(
        signature_comparison_task.run_signature_comparison,
        "delay",
        lambda ref_id, company_id=None: enqueued_tasks.append(ref_id)
    )

    # 1. Create a case
    case = client.post("/cases", json={"case_type": "vendor_invoice"}, headers=auth_headers).json()
    case_id = case["id"]

    # 2. Upload document
    pdf_bytes = PDF
    doc = client.post(
        f"/cases/{case_id}/documents",
        headers=auth_headers,
        files={"file": ("invoice.pdf", pdf_bytes, "application/pdf")},
    ).json()
    doc_id = doc["id"]

    # 3. Create in-case signature reference (is_library=False)
    payload_incase = {
        "person_name": "Alice Signer",
        "bounding_box": {"page": 1, "x": 0.1, "y": 0.2, "width": 0.3, "height": 0.15},
        "is_library": False,
    }
    res = client.post(
        f"/cases/{case_id}/documents/{doc_id}/signature-references",
        json=payload_incase,
        headers=auth_headers,
    )
    assert res.status_code == 201
    data = res.json()
    assert data["person_name"] == "Alice Signer"
    assert data["is_library"] is False
    assert data["source_case_id"] == case_id
    assert data["source_document_id"] == doc_id
    assert len(enqueued_tasks) == 1
    assert enqueued_tasks[0] == data["id"]

    # 4. Create library signature reference (is_library=True)
    payload_lib = {
        "person_name": "Bob Official",
        "bounding_box": {"page": 1, "x": 0.5, "y": 0.6, "width": 0.2, "height": 0.1},
        "is_library": True,
    }
    res_lib = client.post(
        f"/cases/{case_id}/documents/{doc_id}/signature-references",
        json=payload_lib,
        headers=auth_headers,
    )
    assert res_lib.status_code == 201
    data_lib = res_lib.json()
    assert data_lib["person_name"] == "Bob Official"
    assert data_lib["is_library"] is True
    assert len(enqueued_tasks) == 2

    # 5. List references for case
    list_res = client.get(f"/cases/{case_id}/signature-references", headers=auth_headers)
    assert list_res.status_code == 200
    refs = list_res.json()
    assert len(refs) == 2
    assert {r["person_name"] for r in refs} == {"Alice Signer", "Bob Official"}

def test_signature_matches_list_empty_and_populated(client, auth_headers, db_session):
    # Create case and document directly in DB
    from app.models.case import Case, CaseStatus, CaseType
    from app.models.document import Document
    from app.models.user import User

    user = db_session.query(User).first()
    case_id = uuid.uuid4()
    case = Case(
        id=case_id,
        case_number=f"CASE-{case_id.hex[:8].upper()}",
        case_type=CaseType.vendor_invoice,
        submitted_by_user_id=user.id,
        status=CaseStatus.submitted,
    )
    db_session.add(case)

    doc1_id = uuid.uuid4()
    doc1 = Document(
        id=doc1_id,
        case_id=case_id,
        uploaded_by_user_id=user.id,
        original_filename="doc1.pdf",
        content_type="application/pdf",
        file_size_bytes=100,
        file_hash="hash1",
        blob_storage_path="path1.pdf",
    )
    db_session.add(doc1)

    doc2_id = uuid.uuid4()
    doc2 = Document(
        id=doc2_id,
        case_id=case_id,
        uploaded_by_user_id=user.id,
        original_filename="doc2.pdf",
        content_type="application/pdf",
        file_size_bytes=100,
        file_hash="hash2",
        blob_storage_path="path2.pdf",
    )
    db_session.add(doc2)

    ref = SignatureReference(
        id=uuid.uuid4(),
        person_name="Jane Doe",
        source_case_id=case_id,
        source_document_id=doc1_id,
        created_by=user.id,
        is_library=False,
    )
    db_session.add(ref)
    db_session.commit()

    # Initial check: no matches
    res = client.get(f"/cases/{case_id}/signature-matches", headers=auth_headers)
    assert res.status_code == 200
    assert res.json() == []

    # Add a match
    match = SignatureMatch(
        id=uuid.uuid4(),
        document_id=doc2_id,
        case_id=case_id,
        signature_reference_id=ref.id,
        comparison_scope=ComparisonScope.in_case,
        result=SignatureMatchResult.consistent,
        reasoning="Visually consistent line widths and stroke curves.",
    )
    db_session.add(match)
    db_session.commit()

    res = client.get(f"/cases/{case_id}/signature-matches", headers=auth_headers)
    assert res.status_code == 200
    matches = res.json()
    assert len(matches) == 1
    assert matches[0]["reference_person_name"] == "Jane Doe"
    assert matches[0]["result"] == "consistent"
    assert "Visually consistent" in matches[0]["result_label"]
    assert matches[0]["comparison_scope"] == "in_case"
