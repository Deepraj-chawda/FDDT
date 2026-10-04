"""Per-company upload limits (companies.max_file_size_mb / max_zip_size_mb,
app/services/upload_limits.py): set by a platform admin, read fresh on every
upload by both the single-file and the bulk-zip path, audited old → new."""
import io
import os
import uuid

import pymupdf
import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.db import tenancy
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import UserRole
from app.services import bulk_upload_service
from app.tasks.bulk_upload_task import ingest_bulk_upload
from tests.conftest import headers_for, make_company, make_user
from tests.sample_files import make_zip

MB = 1024 * 1024


@pytest.fixture(scope="module")
def pdf_15mb() -> bytes:
    """A real, parseable ~15 MB PDF (an incompressible scanned-style page)."""
    side = 2300
    noise = Image.frombytes("RGB", (side, side), os.urandom(side * side * 3))
    png = io.BytesIO()
    noise.save(png, format="PNG", compress_level=1)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=png.getvalue())
    page.insert_text((72, 72), "Scanned invoice")
    data = doc.tobytes(deflate=True)
    doc.close()
    assert 14 * MB < len(data) < 17 * MB, len(data)
    return data


@pytest.fixture
def ingest_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(ingest_bulk_upload, "delay", lambda *a, **k: calls.append(a))
    return calls


@pytest.fixture
def two_companies(db_session, company):
    """A at the defaults; B with a user of its own."""
    other = make_company(db_session, "Big Customer Ltd")
    b_user = make_user(db_session, "uploader@bigcustomer.example", UserRole.user, company_id=other.id)
    return company, other, headers_for(b_user)


def _patch(client, headers, company_id, **limits):
    return client.patch(f"/platform/companies/{company_id}", json=limits, headers=headers)


def _new_case(client, headers):
    response = client.post("/cases", json={"case_type": "vendor_invoice"}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _upload(client, headers, case_id, content, name="scan.pdf"):
    return client.post(
        f"/cases/{case_id}/documents", headers=headers, files={"file": (name, content, "application/pdf")}
    )


def _post_zip(client, headers, data):
    return client.post(
        "/bulk-uploads", params={"case_type": "vendor_invoice", "filename": "z.zip"}, content=data,
        headers={**headers, "Content-Type": "application/zip"},
    )


def _reload(db_session, company_id) -> Company:
    state = tenancy.snapshot(db_session)
    tenancy.bind_platform(db_session)
    try:
        db_session.expire_all()
        return db_session.get(Company, company_id)
    finally:
        tenancy.restore(db_session, state)


# ---------------------------------------------------------------------------


def test_a_15mb_file_is_rejected_for_a_default_company_and_accepted_for_a_20mb_one(
    client, db_session, two_companies, plain_headers, platform_admin_headers, pdf_15mb
):
    company_a, company_b, b_headers = two_companies
    response = _patch(client, platform_admin_headers, company_b.id, max_file_size_mb=20, max_zip_size_mb=500)
    assert response.status_code == 200, response.text
    assert (response.json()["max_file_size_mb"], response.json()["max_zip_size_mb"]) == (20, 500)

    rejected = _upload(client, plain_headers, _new_case(client, plain_headers), pdf_15mb)
    assert rejected.status_code == 413
    detail = rejected.json()["detail"]
    assert detail["code"] == "file_too_large"
    assert detail["max_size_bytes"] == 10 * MB
    assert "maximum allowed size is 10.0 MB" in detail["message"]

    accepted = _upload(client, b_headers, _new_case(client, b_headers), pdf_15mb)
    assert accepted.status_code == 201, accepted.text


def test_the_bulk_path_enforces_the_same_per_company_file_limit(
    client, db_session, fake_storage, two_companies, plain_headers, platform_admin_headers, pdf_15mb,
    ingest_calls, monkeypatch,
):
    company_a, company_b, b_headers = two_companies
    _patch(client, platform_admin_headers, company_b.id, max_file_size_mb=20)
    monkeypatch.setattr(bulk_upload_service, "enqueue_document_pipeline", lambda d, c: None)
    data = make_zip({"case-1/scan.pdf": pdf_15mb})

    a = _post_zip(client, plain_headers, data)
    assert a.status_code == 202
    a_file = a.json()["cases"][0]["files"][0]
    assert a_file["code"] == "file_too_large"
    assert "maximum allowed size is 10.0 MB" in a_file["message"]

    b = _post_zip(client, b_headers, data)
    assert b.status_code == 202
    assert b.json()["cases"][0]["files"][0]["status"] == "pending"  # passed the structural size check
    # Ingestion (the Celery task) reads B's limit from the same source.
    state = tenancy.snapshot(db_session)
    tenancy.bind_company(db_session, company_b.id)  # the task's session is B's
    try:
        bulk_upload_service.ingest(db_session, fake_storage, uuid.UUID(b.json()["id"]), company_b.id)
    finally:
        tenancy.restore(db_session, state)
    detail = client.get(f"/bulk-uploads/{b.json()['id']}", headers=b_headers).json()
    assert detail["cases"][0]["status"] == "created"
    assert detail["cases"][0]["files"][0]["status"] == "accepted"


def test_the_zip_limit_is_per_company_and_named_in_the_message(
    client, db_session, two_companies, plain_headers, platform_admin_headers, ingest_calls, fake_storage
):
    company_a, company_b, b_headers = two_companies
    _patch(client, platform_admin_headers, company_a.id, max_zip_size_mb=1)
    data = make_zip({"case-1/a.pdf": b"%PDF-1.7\n" + os.urandom(int(1.5 * MB))})

    a = _post_zip(client, plain_headers, data)
    assert a.status_code == 413
    assert a.json()["detail"]["max_size_bytes"] == 1 * MB
    assert "maximum allowed size is 1.0 MB" in a.json()["detail"]["message"]
    assert _post_zip(client, b_headers, data).status_code == 202  # B still at 300 MB


@pytest.mark.parametrize("role", [UserRole.user, UserRole.reviewer_l1, UserRole.reviewer_l2])
def test_no_company_role_may_change_the_limits(client, db_session, company, role):
    headers = headers_for(make_user(db_session, f"{role.value}@limits.example", role))
    response = _patch(client, headers, company.id, max_file_size_mb=500, max_zip_size_mb=5000)
    assert response.status_code == 403
    assert (_reload(db_session, company.id).max_file_size_mb, _reload(db_session, company.id).max_zip_size_mb) == (10, 300)


def test_a_change_applies_to_the_next_upload_without_signing_in_again(
    client, db_session, company, plain_headers, platform_admin_headers, pdf_15mb
):
    case_id = _new_case(client, plain_headers)
    assert _upload(client, plain_headers, case_id, pdf_15mb).status_code == 413
    assert client.get("/auth/me/upload-limits", headers=plain_headers).json()["max_file_size_mb"] == 10

    _patch(client, platform_admin_headers, company.id, max_file_size_mb=20)

    # Same token as before.
    assert _upload(client, plain_headers, case_id, pdf_15mb).status_code == 201
    limits = client.get("/auth/me/upload-limits", headers=plain_headers).json()
    assert limits == {
        "max_file_size_mb": 20, "max_zip_size_mb": 300,
        "max_file_size_bytes": 20 * MB, "max_zip_size_bytes": 300 * MB,
    }


def test_a_limit_change_is_platform_audited_with_old_and_new_values(
    client, db_session, company, platform_admin_user, platform_admin_headers
):
    _patch(client, platform_admin_headers, company.id, max_file_size_mb=20, max_zip_size_mb=500)
    _patch(client, platform_admin_headers, company.id, max_file_size_mb=20)  # no change: no event

    tenancy.bind_platform(db_session)
    events = db_session.execute(
        select(AuditLog).where(AuditLog.event_type == "company_updated")
    ).scalars().all()
    assert len(events) == 1
    event = events[0]
    assert event.company_id is None  # platform-only: invisible to every company session
    assert event.actor_user_id == platform_admin_user.id
    assert event.event_data["company_id"] == str(company.id)
    assert event.event_data["changes"] == {
        "max_file_size_mb": {"from": 10, "to": 20},
        "max_zip_size_mb": {"from": 300, "to": 500},
    }


def test_new_companies_store_the_defaults_and_later_default_changes_dont_touch_them(
    client, db_session, platform_admin_headers, monkeypatch
):
    first = client.post("/platform/companies", json={"name": "Small Co"}, headers=platform_admin_headers).json()
    assert (first["max_file_size_mb"], first["max_zip_size_mb"]) == (10, 300)

    monkeypatch.setattr(settings, "default_max_file_size_mb", 25)
    monkeypatch.setattr(settings, "default_max_zip_size_mb", 600)
    second = client.post("/platform/companies", json={"name": "Later Co"}, headers=platform_admin_headers).json()
    custom = client.post(
        "/platform/companies", json={"name": "Custom Co", "max_file_size_mb": 50, "max_zip_size_mb": 1000},
        headers=platform_admin_headers,
    ).json()

    listed = {c["name"]: c for c in client.get("/platform/companies", headers=platform_admin_headers).json()}
    assert (listed["Small Co"]["max_file_size_mb"], listed["Small Co"]["max_zip_size_mb"]) == (10, 300)
    assert (second["max_file_size_mb"], second["max_zip_size_mb"]) == (25, 600)
    assert (custom["max_file_size_mb"], custom["max_zip_size_mb"]) == (50, 1000)


def test_limits_must_be_positive_but_have_no_ceiling(client, company, platform_admin_headers):
    assert _patch(client, platform_admin_headers, company.id, max_file_size_mb=0).status_code == 422
    assert _patch(client, platform_admin_headers, company.id, max_zip_size_mb=-5).status_code == 422
    big = _patch(client, platform_admin_headers, company.id, max_file_size_mb=2048, max_zip_size_mb=10240)
    assert big.status_code == 200
    assert (big.json()["max_file_size_mb"], big.json()["max_zip_size_mb"]) == (2048, 10240)


def test_platform_admins_have_no_upload_limits_of_their_own(client, platform_admin_headers):
    assert client.get("/auth/me/upload-limits", headers=platform_admin_headers).status_code == 404
