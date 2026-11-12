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


# --- Discarding an empty case (every upload of a new case was rejected) ---

def test_submitter_discards_their_own_empty_case(client, plain_headers):
    case = client.post("/cases", json={"case_type": "vendor_invoice"}, headers=plain_headers).json()

    assert client.post(f"/cases/{case['id']}/discard", headers=plain_headers).status_code == 204

    # Gone for the company: case page, lists and counts.
    assert client.get(f"/cases/{case['id']}", headers=plain_headers).status_code == 404
    assert all(c["id"] != case["id"] for c in client.get("/cases", headers=plain_headers).json())
    assert client.get("/cases/summary?mine=true", headers=plain_headers).json()["total"] == 0
    # ...and it can't be discarded (or uploaded to) twice.
    assert client.post(f"/cases/{case['id']}/discard", headers=plain_headers).status_code == 404
    rejected = client.post(
        f"/cases/{case['id']}/documents",
        headers=plain_headers,
        files={"file": ("invoice.pdf", make_pdf("late"), "application/pdf")},
    )
    assert rejected.status_code == 404


def test_a_case_with_documents_cannot_be_discarded(client, plain_headers, fake_storage):
    case = client.post("/cases", json={"case_type": "vendor_invoice"}, headers=plain_headers).json()
    uploaded = client.post(
        f"/cases/{case['id']}/documents",
        headers=plain_headers,
        files={"file": ("invoice.pdf", make_pdf("an invoice"), "application/pdf")},
    )
    assert uploaded.status_code == 201

    assert client.post(f"/cases/{case['id']}/discard", headers=plain_headers).status_code == 409
    assert client.get(f"/cases/{case['id']}", headers=plain_headers).status_code == 200


def test_only_the_creator_may_discard_an_empty_case(
    client, plain_headers, reviewer_headers, platform_admin_headers, other_plain_user
):
    from tests.conftest import _headers_for

    case = client.post("/cases", json={"case_type": "vendor_invoice"}, headers=plain_headers).json()
    url = f"/cases/{case['id']}/discard"

    assert client.post(url, headers=reviewer_headers).status_code == 404
    assert client.post(url, headers=_headers_for(other_plain_user)).status_code == 404
    assert client.post(url, headers=platform_admin_headers).status_code == 403
    assert client.get(f"/cases/{case['id']}", headers=plain_headers).status_code == 200


def test_case_list_items_carry_the_uploaded_filenames(client, plain_headers, fake_storage):
    case = client.post("/cases", json={"case_type": "vendor_invoice"}, headers=plain_headers).json()
    empty = client.post("/cases", json={"case_type": "vendor_invoice"}, headers=plain_headers).json()
    for i, name in enumerate(["invoice.pdf", "receipt.pdf", "po.pdf", "delivery-note.pdf"]):
        uploaded = client.post(
            f"/cases/{case['id']}/documents",
            headers=plain_headers,
            files={"file": (name, make_pdf(f"document {i}"), "application/pdf")},
        )
        assert uploaded.status_code == 201

    items = {c["id"]: c for c in client.get("/cases/page?mine=true", headers=plain_headers).json()["items"]}
    # Upload order, capped at three; the count stays the full count.
    assert items[case["id"]]["document_names"] == ["invoice.pdf", "receipt.pdf", "po.pdf"]
    assert items[case["id"]]["document_count"] == 4
    assert items[empty["id"]]["document_names"] == []
