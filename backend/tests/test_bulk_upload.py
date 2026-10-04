"""Bulk upload (app/api/bulk_uploads.py, app/services/bulk_upload_service.py,
app/tasks/bulk_upload_task.py): one zip, one folder per case.

The upload request is driven through the real endpoint. Ingestion, which in
production is the Celery task, is called directly on the test session with the
fake Blob Storage, and every pipeline task it would queue is recorded instead
of sent."""
import io
import os
import uuid
import zipfile

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.bulk_upload import BulkUpload, BulkUploadStatus
from app.models.case import Case, CaseStatus, RiskTier
from app.models.case_risk_assessment import CaseRiskAssessment
from app.models.document import Document, DocumentProcessingStatus
from app.models.user import UserRole
from app.services import bulk_upload_service
from app.services.storage_service import StorageOperationError
from app.tasks.bulk_upload_task import ingest_bulk_upload
from tests.conftest import headers_for, make_company, make_user, set_upload_limits
from tests.sample_files import make_corrupted_pdf, make_encrypted_pdf, make_pdf, make_zip

MB = 1024 * 1024


@pytest.fixture
def ingest_calls(monkeypatch):
    """What the upload endpoint queues for ingestion (one task per zip)."""
    calls: list[tuple] = []
    monkeypatch.setattr(ingest_bulk_upload, "delay", lambda *a, **k: calls.append(a))
    return calls


@pytest.fixture
def enqueued(monkeypatch):
    """Every document whose pipeline ingestion queues, in order."""
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []
    monkeypatch.setattr(
        bulk_upload_service, "enqueue_document_pipeline", lambda d, c: calls.append((d, c))
    )
    return calls


def _post(client, headers, data: bytes, *, filename="claims.zip", case_type="vendor_invoice"):
    return client.post(
        "/bulk-uploads",
        params={"case_type": case_type, "filename": filename},
        content=data,
        headers={**headers, "Content-Type": "application/zip"},
    )


def _ingest(db_session, fake_storage, bulk_id):
    company_id = db_session.info["test_company_id"]
    bulk_upload_service.ingest(db_session, fake_storage, uuid.UUID(str(bulk_id)), company_id)


def _upload_and_ingest(client, headers, db_session, fake_storage, data, **kw):
    response = _post(client, headers, data, **kw)
    assert response.status_code == 202, response.text
    _ingest(db_session, fake_storage, response.json()["id"])
    return response.json()["id"]


def _detail(client, headers, bulk_id):
    response = client.get(f"/bulk-uploads/{bulk_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _doc_blobs(fake_storage):
    return {k: v for k, v in fake_storage.uploads.items() if "/documents/" in k}


def _count(db_session, model):
    return db_session.execute(select(func.count()).select_from(model)).scalar_one()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_each_folder_becomes_a_case_and_each_document_is_queued(
    client, plain_user, plain_headers, db_session, fake_storage, ingest_calls, enqueued
):
    data = make_zip({
        "case-001/invoice.pdf": make_pdf("inv 1"),
        "case-001/receipt.pdf": make_pdf("rcpt 1"),
        "case-002/invoice.pdf": make_pdf("inv 2"),
        "case-010/quote.pdf": make_pdf("quote 10"),
        "case-010/po.pdf": make_pdf("po 10"),
    })
    response = _post(client, plain_headers, data)
    assert response.status_code == 202, response.text
    body = response.json()

    # The request only stored the zip and queued ONE ingestion task.
    assert body["status"] == "queued"
    assert body["case_folder_count"] == 3
    assert [c["folder"] for c in body["cases"]] == ["case-001", "case-002", "case-010"]  # natural order
    assert {c["live_status"] for c in body["cases"]} == {"validating"}
    company_id = db_session.info["test_company_id"]
    assert ingest_calls == [(body["id"], str(company_id))]
    assert _count(db_session, Case) == 0 and enqueued == []
    zip_blobs = [k for k in fake_storage.uploads if "/bulk-uploads/" in k]
    assert len(zip_blobs) == 1 and zip_blobs[0].startswith(f"companies/{company_id}/bulk-uploads/")

    _ingest(db_session, fake_storage, body["id"])

    cases = db_session.execute(select(Case).order_by(Case.reference_label)).scalars().all()
    assert [c.reference_label for c in cases] == ["case-001", "case-002", "case-010"]
    for case in cases:
        assert case.bulk_upload_id == uuid.UUID(body["id"])
        assert case.submitted_by_user_id == plain_user.id  # the uploader owns every case
        assert case.case_type.value == "vendor_invoice"
        assert case.status == CaseStatus.submitted
    documents = db_session.execute(select(Document)).scalars().all()
    assert len(documents) == 5 == len(_doc_blobs(fake_storage))
    assert sorted(d for d, _ in enqueued) == sorted(d.id for d in documents)
    assert {c for _, c in enqueued} == {company_id}

    detail = _detail(client, plain_headers, body["id"])
    assert detail["status"] == "complete"
    assert (detail["cases_created"], detail["cases_failed"]) == (3, 0)
    assert (detail["documents_accepted"], detail["documents_rejected"]) == (5, 0)
    assert detail["progress"]["queued"] == 3 and not detail["settled"]
    assert all(c["case_number"].startswith("CASE-") for c in detail["cases"])

    events = db_session.execute(select(AuditLog.event_type)).scalars().all()
    assert events.count("case_created") == 3 and events.count("document_uploaded") == 5
    assert "bulk_upload_received" in events and "bulk_upload_ingested" in events


# ---------------------------------------------------------------------------
# Partial success
# ---------------------------------------------------------------------------


def test_partial_success_rejects_only_the_bad_files_and_cases(
    client, plain_headers, db_session, fake_storage, ingest_calls, enqueued
):
    data = make_zip({
        "good/a.pdf": make_pdf("good a"),
        "good/b.pdf": make_pdf("good b"),
        "mixed/ok.pdf": make_pdf("mixed ok"),
        "mixed/broken.pdf": make_corrupted_pdf(),
        "mixed/locked.pdf": make_encrypted_pdf(),
        "mixed/huge.pdf": b"%PDF-1.7\n" + b"0" * (11 * MB),
        "mixed/empty.pdf": b"",
        "mixed/notes.pdf": b"just some text renamed to .pdf",
        "nested/top.pdf": make_pdf("nested top"),
        "nested/2025/inner.pdf": make_pdf("nested inner"),
        "allbad/broken.pdf": make_corrupted_pdf(),
        "emptyfolder/": b"",
    })
    response = _post(client, plain_headers, data)
    assert response.status_code == 202, response.text
    upfront = {c["folder"]: c for c in response.json()["cases"]}

    # Visible in the upload response itself, before any processing.
    assert upfront["nested"]["status"] == "failed"
    assert upfront["nested"]["error_code"] == "case_nested_folder"
    assert "'2025'" in upfront["nested"]["error_message"]
    assert [f["status"] for f in upfront["nested"]["files"]] == ["skipped"]
    assert upfront["emptyfolder"]["error_code"] == "case_no_documents"
    early = {f["name"]: f for f in upfront["mixed"]["files"]}
    assert early["huge.pdf"]["code"] == "file_too_large"
    assert early["empty.pdf"]["code"] == "file_empty"

    _ingest(db_session, fake_storage, response.json()["id"])
    detail = _detail(client, plain_headers, response.json()["id"])
    cases = {c["folder"]: c for c in detail["cases"]}

    assert cases["good"]["status"] == "created"
    assert [f["status"] for f in cases["good"]["files"]] == ["accepted", "accepted"]

    assert cases["mixed"]["status"] == "created"  # one valid file is enough
    mixed = {f["name"]: f for f in cases["mixed"]["files"]}
    assert mixed["ok.pdf"]["status"] == "accepted"
    assert mixed["broken.pdf"]["code"] == "file_corrupted"
    assert mixed["locked.pdf"]["code"] == "file_password_protected"
    assert mixed["huge.pdf"]["code"] == "file_too_large"
    assert "10.0 MB" in mixed["huge.pdf"]["message"] and "11.0 MB" in mixed["huge.pdf"]["message"]
    assert mixed["empty.pdf"]["code"] == "file_empty"
    assert mixed["notes.pdf"]["code"] == "unsupported_file_type"

    assert cases["nested"]["status"] == "failed" and cases["nested"]["case_id"] is None
    assert cases["allbad"]["status"] == "failed"
    assert cases["allbad"]["error_code"] == "case_no_valid_documents"
    assert cases["allbad"]["files"][0]["code"] == "file_corrupted"

    assert (detail["cases_created"], detail["cases_failed"]) == (2, 3)
    assert (detail["documents_accepted"], detail["documents_rejected"]) == (3, 6)
    # Rejected files never reach Blob Storage, the documents table or a worker.
    assert _count(db_session, Case) == 2
    assert _count(db_session, Document) == 3 == len(_doc_blobs(fake_storage)) == len(enqueued)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def test_same_looking_folder_names_are_separate_cases(
    client, plain_headers, db_session, fake_storage, ingest_calls, enqueued
):
    # Two folders a filesystem might merge (they differ only in case) and the
    # same folder names again in a second zip: labels are not identities.
    data = make_zip({"Invoice/a.pdf": make_pdf("upper"), "invoice/a.pdf": make_pdf("lower")})
    _upload_and_ingest(client, plain_headers, db_session, fake_storage, data)
    _upload_and_ingest(client, plain_headers, db_session, fake_storage, data, filename="claims-again.zip")

    cases = db_session.execute(select(Case)).scalars().all()
    assert len(cases) == 4
    assert sorted(c.reference_label for c in cases) == ["Invoice", "Invoice", "invoice", "invoice"]
    assert len({c.id for c in cases}) == len({c.case_number for c in cases}) == 4


@pytest.mark.parametrize("name_style", ["flag", "raw_utf8", "unicode_extra"])
def test_arabic_folder_and_file_names(
    client, plain_headers, db_session, fake_storage, ingest_calls, enqueued, name_style
):
    folder, filename = "مطالبة-٢٠٢٦", "فاتورة ضريبية.pdf"
    data = make_zip({f"{folder}/{filename}": make_pdf("arabic"), f"{folder}/receipt.pdf": make_pdf("r")},
                    name_style=name_style)
    bulk_id = _upload_and_ingest(client, plain_headers, db_session, fake_storage, data)

    case = db_session.execute(select(Case)).scalar_one()
    assert case.reference_label == folder
    names = sorted(d.original_filename for d in db_session.execute(select(Document)).scalars())
    assert names == sorted([filename, "receipt.pdf"])
    detail = _detail(client, plain_headers, bulk_id)
    assert detail["cases"][0]["folder"] == folder
    assert {f["name"] for f in detail["cases"][0]["files"]} == {filename, "receipt.pdf"}
    assert all(k.endswith(".pdf") for k in _doc_blobs(fake_storage))


def test_wrapper_folder_and_os_clutter(
    client, plain_headers, db_session, fake_storage, ingest_calls, enqueued
):
    data = make_zip({
        "claims-oct/case-1/a.pdf": make_pdf("w1"),
        "claims-oct/case-2/b.pdf": make_pdf("w2"),
        "claims-oct/case-2/.DS_Store": b"junk",
        "__MACOSX/claims-oct/case-1/._a.pdf": b"junk",
    })
    bulk_id = _upload_and_ingest(client, plain_headers, db_session, fake_storage, data)
    detail = _detail(client, plain_headers, bulk_id)
    assert detail["wrapper_folder"] == "claims-oct"
    assert [c["folder"] for c in detail["cases"]] == ["case-1", "case-2"]
    assert [len(c["files"]) for c in detail["cases"]] == [1, 1]


def test_loose_files_at_the_root_are_listed_as_ignored(
    client, plain_headers, db_session, fake_storage, ingest_calls, enqueued
):
    data = make_zip({"readme.txt": b"hi", "case-1/a.pdf": make_pdf("x")})
    bulk_id = _upload_and_ingest(client, plain_headers, db_session, fake_storage, data)
    detail = _detail(client, plain_headers, bulk_id)
    assert detail["ignored_entries"] == ["readme.txt"]
    assert detail["cases_created"] == 1


# ---------------------------------------------------------------------------
# Zip-level rejections (nothing stored, nothing queued)
# ---------------------------------------------------------------------------


def _assert_zip_rejected(response, status, code, db_session, fake_storage, ingest_calls):
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert fake_storage.uploads == {}
    assert ingest_calls == []
    assert _count(db_session, BulkUpload) == 0
    return detail


def test_zip_over_the_limit_is_rejected_stating_both_sizes(
    client, plain_headers, db_session, company, fake_storage, ingest_calls
):
    set_upload_limits(db_session, company.id, zip_mb=1)
    data = make_zip({"case-1/a.pdf": make_pdf("big", filler=0) + os.urandom(int(1.5 * MB))})
    detail = _assert_zip_rejected(
        _post(client, plain_headers, data), 413, "zip_too_large", db_session, fake_storage, ingest_calls
    )
    assert detail["size_bytes"] == len(data)
    assert detail["max_size_bytes"] == 1 * MB
    assert "1.0 MB" in detail["message"]


def test_default_limits(company):
    assert (company.max_file_size_mb, company.max_zip_size_mb) == (10, 300)


@pytest.mark.parametrize(
    "data,status,code",
    [
        (b"", 400, "zip_empty"),
        (b"%PDF-1.7 this is a pdf, not a zip", 415, "not_a_zip"),
        (make_zip({"case-1/a.pdf": make_pdf("cut")})[:200], 422, "zip_corrupted"),
        (make_zip({"a.pdf": make_pdf("loose"), "b.pdf": make_pdf("loose2")}), 422, "zip_no_case_folders"),
    ],
    ids=["empty", "not-a-zip", "truncated", "no-case-folders"],
)
def test_zip_level_rejections(client, plain_headers, db_session, fake_storage, ingest_calls, data, status, code):
    _assert_zip_rejected(_post(client, plain_headers, data), status, code, db_session, fake_storage, ingest_calls)


def test_too_many_entries(client, plain_headers, db_session, fake_storage, ingest_calls, monkeypatch):
    monkeypatch.setattr(settings, "bulk_upload_max_entries", 3)
    data = make_zip({f"case-{i}/a.pdf": make_pdf(str(i)) for i in range(4)})
    _assert_zip_rejected(
        _post(client, plain_headers, data), 422, "zip_too_many_entries", db_session, fake_storage, ingest_calls
    )


def test_case_count_warning_is_soft(client, plain_headers, db_session, fake_storage, ingest_calls, monkeypatch):
    monkeypatch.setattr(settings, "bulk_upload_case_warning_threshold", 2)
    data = make_zip({f"case-{i}/a.pdf": make_pdf(str(i)) for i in range(3)})
    response = _post(client, plain_headers, data)
    assert response.status_code == 202  # accepted anyway
    assert "3 cases" in response.json()["warnings"][0]


# ---------------------------------------------------------------------------
# No batch blocking, resumability
# ---------------------------------------------------------------------------


def test_each_case_is_queued_before_the_next_is_even_extracted(
    client, reviewer_headers, plain_headers, db_session, fake_storage, ingest_calls, monkeypatch
):
    timeline: list[tuple[str, str]] = []
    real_validate = bulk_upload_service._validate_case_files

    def tracing_validate(zf, infos, case, max_bytes):
        timeline.append(("extract", case["folder"]))
        return real_validate(zf, infos, case, max_bytes)

    monkeypatch.setattr(bulk_upload_service, "_validate_case_files", tracing_validate)
    monkeypatch.setattr(
        bulk_upload_service, "enqueue_document_pipeline",
        lambda d, c: timeline.append(("queue", db_session.get(Document, d).original_filename)),
    )
    data = make_zip({
        "c1/one.pdf": make_pdf("1"), "c2/two.pdf": make_pdf("2"), "c3/three.pdf": make_pdf("3"),
    })
    bulk_id = _upload_and_ingest(client, plain_headers, db_session, fake_storage, data)
    assert timeline == [
        ("extract", "c1"), ("queue", "one.pdf"),
        ("extract", "c2"), ("queue", "two.pdf"),
        ("extract", "c3"), ("queue", "three.pdf"),
    ]

    # Case c1 finishes (its documents processed and scored) while c2 and c3
    # are still queued: the summary shows each on its own.
    first = db_session.execute(select(Case).where(Case.reference_label == "c1")).scalar_one()
    second = db_session.execute(select(Case).where(Case.reference_label == "c2")).scalar_one()
    for doc in db_session.execute(select(Document).where(Document.case_id == first.id)).scalars():
        doc.processing_status = DocumentProcessingStatus.complete
    for doc in db_session.execute(select(Document).where(Document.case_id == second.id)).scalars():
        doc.processing_status = DocumentProcessingStatus.processing
    db_session.add(CaseRiskAssessment(
        case_id=first.id, raw_score=70.0, score=70, tier=RiskTier.high, triggered_reasons=[],
        risk_rules_version_snapshot={}, evidence_fingerprint="f" * 64,
    ))
    db_session.commit()

    by_folder = {c["folder"]: c for c in _detail(client, reviewer_headers, bulk_id)["cases"]}
    assert by_folder["c1"]["live_status"] == "flagged"
    assert by_folder["c2"]["live_status"] == "processing"
    assert by_folder["c3"]["live_status"] == "queued"
    # A submitter never sees risk signals: the same case reads "done".
    by_folder = {c["folder"]: c for c in _detail(client, plain_headers, bulk_id)["cases"]}
    assert by_folder["c1"]["live_status"] == "done"
    assert by_folder["c1"]["flag"]["flag"] == "pending"


def test_a_crash_mid_zip_resumes_without_duplicating_cases(
    client, plain_headers, db_session, fake_storage, ingest_calls, enqueued, monkeypatch
):
    data = make_zip({"c1/a.pdf": make_pdf("a"), "c2/b.pdf": make_pdf("b"), "c3/c.pdf": make_pdf("c")})
    response = _post(client, plain_headers, data)
    bulk_id = response.json()["id"]

    real_store = bulk_upload_service.store_original
    calls = {"n": 0}

    def flaky_store(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise StorageOperationError("blob storage hiccup")
        return real_store(*args, **kwargs)

    monkeypatch.setattr(bulk_upload_service, "store_original", flaky_store)
    with pytest.raises(StorageOperationError):
        _ingest(db_session, fake_storage, bulk_id)
    db_session.rollback()
    assert _count(db_session, Case) == 1
    bulk = db_session.get(BulkUpload, uuid.UUID(bulk_id))
    assert bulk.status == BulkUploadStatus.ingesting

    _ingest(db_session, fake_storage, bulk_id)  # the task's retry
    assert sorted(c.reference_label for c in db_session.execute(select(Case)).scalars()) == ["c1", "c2", "c3"]
    assert len(enqueued) == 3
    _ingest(db_session, fake_storage, bulk_id)  # a stray redelivery after completion
    assert _count(db_session, Case) == 3 and len(enqueued) == 3


# ---------------------------------------------------------------------------
# Roles and tenancy
# ---------------------------------------------------------------------------


def test_every_company_role_may_bulk_upload_but_not_platform_admins(
    client, plain_headers, reviewer_headers, l2_reviewer_headers, platform_admin_headers, ingest_calls
):
    data = make_zip({"c1/a.pdf": make_pdf("roles")})
    for headers in (plain_headers, reviewer_headers, l2_reviewer_headers):
        assert _post(client, headers, data).status_code == 202
    assert _post(client, platform_admin_headers, data).status_code == 403


def test_visibility_follows_the_case_rules(
    client, db_session, plain_headers, other_plain_headers, reviewer_headers, platform_admin_headers,
    fake_storage, ingest_calls, enqueued,
):
    bulk_id = _upload_and_ingest(client, plain_headers, db_session, fake_storage, make_zip({"c1/a.pdf": make_pdf("v")}))
    assert client.get(f"/bulk-uploads/{bulk_id}", headers=other_plain_headers).status_code == 404
    assert client.get("/bulk-uploads", headers=other_plain_headers).json() == []
    assert client.get(f"/bulk-uploads/{bulk_id}", headers=reviewer_headers).status_code == 200
    assert len(client.get("/bulk-uploads", headers=reviewer_headers).json()) == 1
    # Platform admin: read-only, per company.
    company_id = db_session.info["test_company_id"]
    assert client.get(f"/bulk-uploads/{bulk_id}", headers=platform_admin_headers).status_code == 400
    assert client.get(
        f"/bulk-uploads/{bulk_id}", params={"company_id": str(company_id)}, headers=platform_admin_headers
    ).status_code == 200

    other = make_company(db_session, "Other Co")
    outsider = make_user(db_session, "outsider@example.com", UserRole.reviewer_l2, company_id=other.id)
    assert client.get(f"/bulk-uploads/{bulk_id}", headers=headers_for(outsider)).status_code == 404


def test_an_oversized_zip_is_refused_from_its_content_length_before_the_body_is_read(
    client, plain_user, company, fake_storage, ingest_calls
):
    """The size verdict comes from the Content-Length header: the app answers
    413 without ever pulling a byte of the body (receive() is never called)."""
    import asyncio
    import json

    from app.core.security import create_access_token
    from app.api.auth import token_claims
    from app.main import app

    too_big = company.max_zip_size_mb * MB + 1
    token = create_access_token(subject=str(plain_user.id), extra_claims=token_claims(plain_user))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": "/bulk-uploads", "raw_path": b"/bulk-uploads",
        "query_string": b"case_type=vendor_invoice&filename=huge.zip", "root_path": "",
        "headers": [
            (b"host", b"testserver"), (b"content-type", b"application/zip"),
            (b"content-length", str(too_big).encode()), (b"authorization", f"Bearer {token}".encode()),
        ],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    body_reads = []
    sent = []

    async def receive():
        body_reads.append(1)
        return {"type": "http.request", "body": b"PK\x03\x04", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))
    assert status == 413
    assert body["detail"]["code"] == "zip_too_large"
    assert body["detail"]["size_bytes"] == too_big
    assert "300.0 MB" in body["detail"]["message"]
    assert body_reads == []
    assert fake_storage.uploads == {} and ingest_calls == []
