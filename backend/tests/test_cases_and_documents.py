import hashlib

from app.services.storage_service import StorageOperationError
from tests.sample_files import PDF, make_pdf


def test_create_case_requires_auth(client):
    response = client.post("/cases", json={"case_type": "vendor_invoice"})
    assert response.status_code == 401


def test_create_case(client, auth_headers, seeded_user):
    response = client.post(
        "/cases", json={"case_type": "vendor_invoice"}, headers=auth_headers
    )
    assert response.status_code == 201
    body = response.json()
    assert body["case_type"] == "vendor_invoice"
    assert body["status"] == "submitted"
    assert body["submitted_by_user_id"] == str(seeded_user.id)
    assert body["case_number"].startswith("CASE-")


def test_upload_document(client, auth_headers, fake_storage):
    case = client.post(
        "/cases", json={"case_type": "vendor_invoice"}, headers=auth_headers
    ).json()

    file_content = make_pdf("hello world, this is an invoice")
    response = client.post(
        f"/cases/{case['id']}/documents",
        headers=auth_headers,
        files={"file": ("invoice.pdf", file_content, "application/pdf")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["case_id"] == case["id"]
    assert body["original_filename"] == "invoice.pdf"
    assert body["file_hash"] == hashlib.sha256(file_content).hexdigest()
    # Signed (browser-fetchable) URL, not the bare durable one.
    assert body["file_url"].split("?")[0].endswith(".pdf")
    assert "?fake-sas-token" in body["file_url"]
    assert body["file_size_bytes"] == len(file_content)

    # Actually went through the storage abstraction, not a shortcut.
    assert len(fake_storage.uploads) == 1
    uploaded_path, uploaded_bytes = next(iter(fake_storage.uploads.items()))
    assert case["id"] in uploaded_path
    assert uploaded_bytes == file_content


def test_upload_pdf_document(client, auth_headers, fake_storage):
    """A real (minimal, valid) PDF — not just arbitrary bytes with a .pdf
    name — round-trips through hashing/upload/DB record correctly."""
    case = client.post(
        "/cases", json={"case_type": "vendor_invoice"}, headers=auth_headers
    ).json()

    pdf_bytes = make_pdf("A real PDF")
    response = client.post(
        f"/cases/{case['id']}/documents",
        headers=auth_headers,
        files={"file": ("real.pdf", pdf_bytes, "application/pdf")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["content_type"] == "application/pdf"
    assert body["file_hash"] == hashlib.sha256(pdf_bytes).hexdigest()
    uploaded_bytes = next(iter(fake_storage.uploads.values()))
    assert uploaded_bytes == pdf_bytes


def test_upload_document_unknown_case_404(client, auth_headers):
    response = client.post(
        "/cases/00000000-0000-0000-0000-000000000000/documents",
        headers=auth_headers,
        files={"file": ("x.pdf", PDF, "application/pdf")},
    )
    assert response.status_code == 404


def test_upload_document_storage_failure_is_clean_502(client, auth_headers, fake_storage):
    case = client.post(
        "/cases", json={"case_type": "vendor_invoice"}, headers=auth_headers
    ).json()

    def _boom(blob_path, content, content_type=None):
        raise StorageOperationError("simulated Azure outage")

    fake_storage.upload = _boom

    response = client.post(
        f"/cases/{case['id']}/documents",
        headers=auth_headers,
        files={"file": ("x.pdf", PDF, "application/pdf")},
    )
    assert response.status_code == 502
    assert "simulated Azure outage" in response.json()["detail"]
