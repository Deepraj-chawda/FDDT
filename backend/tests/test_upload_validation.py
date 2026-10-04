"""Upload validation (app/services/upload_validation.py), through the real
upload endpoint: each rejection has its own code and message, and a rejected
file never reaches Blob Storage, the documents table, the usage counters or a
Celery task."""
import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.document import Document
from app.services.upload_validation import UploadRejected, is_pdf, validate_upload
from tests.sample_files import PDF, make_encrypted_pdf, make_image, make_pdf

LIMIT = 10 * 1024 * 1024
from app.tasks.document_processing import process_document
from app.tasks.duplicate_check_task import run_duplicate_check_task
from app.tasks.metadata_forensics_task import run_metadata_forensics
from app.tasks.signature_detection_task import run_signature_detection
from app.tasks.tampering_checks_task import run_tampering_checks
from app.tasks.visual_inconsistency_task import run_visual_inconsistency_review_task

# Every pipeline task an upload enqueues (app/services/document_intake.py).
TASKS = {
    "process_document": process_document,
    "run_metadata_forensics": run_metadata_forensics,
    "run_tampering_checks": run_tampering_checks,
    "run_duplicate_check_task": run_duplicate_check_task,
    "run_visual_inconsistency_review_task": run_visual_inconsistency_review_task,
    "run_signature_detection": run_signature_detection,
}


@pytest.fixture
def enqueued(monkeypatch):
    """Record every pipeline task the upload endpoint enqueues."""
    calls: list[str] = []
    for name, task in TASKS.items():
        monkeypatch.setattr(task, "delay", lambda *a, _n=name, **k: calls.append(_n))
    return calls


@pytest.fixture
def case_id(client, auth_headers):
    return client.post("/cases", json={"case_type": "vendor_invoice"}, headers=auth_headers).json()["id"]


def _upload(client, headers, case_id, filename, content, declared="application/pdf"):
    return client.post(
        f"/cases/{case_id}/documents", headers=headers, files={"file": (filename, content, declared)}
    )


def _assert_rejected(response, status, code, fake_storage, enqueued, db_session):
    assert response.status_code == status, response.text
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert fake_storage.uploads == {}, "a rejected file must never reach Blob Storage"
    assert enqueued == [], "a rejected file must never trigger a Celery task"
    assert db_session.execute(select(func.count()).select_from(Document)).scalar_one() == 0
    assert db_session.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.event_type == "document_uploaded")
    ).scalar_one() == 0
    return detail


def test_a_new_companys_default_limit_is_10_mb(company):
    assert settings.default_max_file_size_mb == 10
    assert company.max_file_size_mb * 1024 * 1024 == LIMIT


def test_empty_file_is_rejected_with_its_own_message(client, auth_headers, case_id, fake_storage, enqueued, db_session):
    detail = _assert_rejected(
        _upload(client, auth_headers, case_id, "empty.pdf", b""), 400, "file_empty", fake_storage, enqueued, db_session
    )
    assert detail["message"] == "The file is empty (0 bytes)."
    assert "maximum" not in detail["message"]


def test_file_over_10_mb_is_rejected_stating_limit_and_actual_size(
    client, auth_headers, case_id, fake_storage, enqueued, db_session
):
    content = b"%PDF-1.7\n" + b"0" * (12 * 1024 * 1024)  # 12.0 MB
    detail = _assert_rejected(
        _upload(client, auth_headers, case_id, "big.pdf", content), 413, "file_too_large", fake_storage, enqueued, db_session
    )
    assert detail["size_bytes"] == len(content)
    assert detail["max_size_bytes"] == LIMIT
    assert "12.0 MB" in detail["message"] and "10.0 MB" in detail["message"]


def test_one_byte_over_the_limit_is_rejected(client, auth_headers, case_id, fake_storage, enqueued, db_session):
    content = b"%PDF-1.7\n" + b"0" * (LIMIT + 1 - 9)
    detail = _assert_rejected(
        _upload(client, auth_headers, case_id, "big.pdf", content), 413, "file_too_large", fake_storage, enqueued, db_session
    )
    assert detail["size_bytes"] == LIMIT + 1


@pytest.mark.parametrize(
    "filename, content, found",
    [
        ("notes.pdf", b"just some plain text, renamed to .pdf\n" * 20, None),
        ("archive.pdf", b"PK\x03\x04" + b"\x00" * 200, "a ZIP archive or Office document"),
        ("page.pdf", b"<html><body>invoice</body></html>", None),
        ("scan.pdf", make_image("PNG"), "a PNG image"),  # a real image renamed to .pdf
        ("scan.jpg", make_image("JPEG"), "a JPEG image"),  # images are no longer accepted at all
        ("scan.tiff", make_image("TIFF"), "a TIFF image"),
        ("anim.gif", b"GIF89a" + b"\x00" * 100, "a GIF image"),
    ],
    ids=["text", "zip", "html", "png-as-pdf", "jpeg", "tiff", "gif"],
)
def test_anything_but_a_pdf_is_rejected_by_magic_bytes(
    client, auth_headers, case_id, fake_storage, enqueued, db_session, filename, content, found
):
    detail = _assert_rejected(
        _upload(client, auth_headers, case_id, filename, content, "application/pdf"), 415, "unsupported_file_type",
        fake_storage, enqueued, db_session,
    )
    assert "Only PDF files are accepted" in detail["message"]
    if found:
        assert detail["message"].startswith(f"This file is {found}.")


@pytest.mark.parametrize(
    "filename, extension",
    [("invoice.png", ".png"), ("invoice.docx", ".docx"), ("invoice.JPG", ".jpg")],
)
def test_a_pdf_with_another_types_extension_is_rejected(
    client, auth_headers, case_id, fake_storage, enqueued, db_session, filename, extension
):
    detail = _assert_rejected(
        _upload(client, auth_headers, case_id, filename, PDF), 415, "file_type_mismatch",
        fake_storage, enqueued, db_session,
    )
    assert detail["extension"] == extension


@pytest.mark.parametrize(
    "content",
    [
        make_pdf("Truncated", pages=3, filler=3000)[:1500],  # valid header, cut off
        make_pdf("Truncated", pages=3, filler=3000)[:-400],  # loses the xref/trailer
        b"%PDF-1.7\n" + bytes(range(256)) * 20,  # valid header, garbage body
    ],
    ids=["truncated-early", "truncated-tail", "garbage-body"],
)
def test_corrupted_pdf_is_rejected_by_the_parse_check(
    client, auth_headers, case_id, fake_storage, enqueued, db_session, content
):
    detail = _assert_rejected(
        _upload(client, auth_headers, case_id, "broken.pdf", content), 422, "file_corrupted",
        fake_storage, enqueued, db_session,
    )
    assert detail["message"] == "The file appears to be corrupted or unreadable."


def test_password_protected_pdf_gets_the_password_message_not_corruption(
    client, auth_headers, case_id, fake_storage, enqueued, db_session
):
    detail = _assert_rejected(
        _upload(client, auth_headers, case_id, "locked.pdf", make_encrypted_pdf()), 422, "file_password_protected",
        fake_storage, enqueued, db_session,
    )
    assert detail["message"] == "This file is password-protected. Please remove the password and re-upload."


def test_valid_pdf_passes_and_enters_the_pipeline_unchanged(client, auth_headers, case_id, fake_storage, enqueued):
    content = make_pdf("A normal invoice", pages=2)
    response = _upload(client, auth_headers, case_id, "invoice.pdf", content)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["content_type"] == "application/pdf"
    assert body["file_size_bytes"] == len(content)
    assert list(fake_storage.uploads.values()) == [content]  # stored byte-for-byte
    assert sorted(enqueued) == sorted(TASKS)  # the same six tasks as before


def test_stored_content_type_is_the_detected_one_not_the_declared_one(client, auth_headers, case_id, fake_storage):
    response = _upload(client, auth_headers, case_id, "invoice.pdf", PDF, declared="application/octet-stream")
    assert response.status_code == 201
    assert response.json()["content_type"] == "application/pdf"


@pytest.mark.parametrize(
    "filename",
    ["invoice.PDF", "Invoice 12.03.2026", "invoice"],
    ids=["upper-case", "dotted-name", "no-extension"],  # the suffix ".2026" is not a type claim
)
def test_pdfs_with_unusual_names_are_accepted(client, auth_headers, case_id, filename):
    response = _upload(client, auth_headers, case_id, filename, PDF, declared="application/octet-stream")
    assert response.status_code == 201, response.text
    assert response.json()["content_type"] == "application/pdf"


def test_owner_password_only_pdf_is_accepted(client, auth_headers, case_id):
    """Restrictions-only (owner) password: it opens without a password, so
    it is not 'password-protected' for our purposes."""
    response = _upload(client, auth_headers, case_id, "restricted.pdf", make_encrypted_pdf(user_password=""))
    assert response.status_code == 201, response.text


# --- service-level ---------------------------------------------------------

def test_is_pdf_uses_header_bytes():
    assert is_pdf(PDF)
    assert is_pdf(b"junk before the header\n" + PDF)  # within the first 1024 bytes
    assert not is_pdf(b"x" * 1100 + PDF)
    assert not is_pdf(make_image("PNG"))
    assert not is_pdf(b"hello")


def test_pdf_with_leading_junk_is_accepted_and_parsed():
    result = validate_upload(b"%junk-line\n" + PDF, "invoice.pdf", max_bytes=LIMIT)
    assert result.content_type == "application/pdf" and result.page_count == 1


def test_check_order_size_before_type():
    """An oversized non-document is reported as too large, not wrong type."""
    with pytest.raises(UploadRejected) as exc:
        validate_upload(b"x" * 101, "a.txt", max_bytes=100)
    assert exc.value.code == "file_too_large"
