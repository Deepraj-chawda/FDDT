"""
Unit tests for the duplicate-check Celery task
(app/tasks/duplicate_check_task.py). Same in-memory-SQLite pattern as
tests/test_metadata_forensics_task.py/test_tampering_checks_task.py —
monkeypatches the task module's `SessionLocal` and calls the task
function directly (bypassing `.delay()`/a real broker), and monkeypatches
storage to hand back an in-memory PDF instead of touching real Azure Blob
Storage.
"""
import io
import uuid
from unittest.mock import MagicMock

import pikepdf
import pytest

from tests.conftest import tenant_task_factory
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.tasks.duplicate_check_task as duplicate_check_task_module
from app.models import Base
from app.models.audit_log import AuditLog
from app.models.case import Case, CaseStatus, CaseType
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.models.document_page_hash import DocumentPageHash
from app.models.user import User, UserRole


def _minimal_pdf_bytes() -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(300, 300))
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


@pytest.fixture()
def task_session_factory(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = tenant_task_factory(monkeypatch, engine)
    monkeypatch.setattr(duplicate_check_task_module, "SessionLocal", factory)
    return factory


def _seed_document(session_factory, *, content_type="application/pdf", filename="invoice.pdf"):
    session = session_factory()
    try:
        user = User(
            email=f"uploader-{uuid.uuid4()}@example.com",
            hashed_password="x",
            role=UserRole.user,
            is_active=True,
        )
        session.add(user)
        session.flush()
        case = Case(
            case_number=f"CASE-DUPCHECK-{filename}",
            case_type=CaseType.vendor_invoice,
            submitted_by_user_id=user.id,
            status=CaseStatus.submitted,
        )
        session.add(case)
        session.flush()
        document = Document(
            case_id=case.id,
            uploaded_by_user_id=user.id,
            original_filename=filename,
            blob_storage_path=f"https://fake.blob.core.windows.net/documents/{filename}",
            file_hash="0" * 64,
            content_type=content_type,
        )
        session.add(document)
        session.commit()
        session.refresh(document)
        return document.id
    finally:
        session.close()


def _patch_storage(monkeypatch, pdf_bytes: bytes):
    fake_storage = MagicMock()
    fake_storage.download_bytes.return_value = pdf_bytes
    monkeypatch.setattr(duplicate_check_task_module, "get_storage_service_for_task", lambda: fake_storage)
    return fake_storage


def test_writes_completed_check_row_and_stores_page_hashes(task_session_factory, monkeypatch):
    document_id = _seed_document(task_session_factory)
    _patch_storage(monkeypatch, _minimal_pdf_bytes())

    duplicate_check_task_module.run_duplicate_check_task(str(document_id))

    session = task_session_factory()
    check = session.query(DocumentCheck).filter_by(document_id=document_id).one()
    assert check.check_type == DocumentCheckType.duplicate_detection
    assert check.status == DocumentCheckStatus.completed
    assert check.result == {"result": "pass", "details": []}

    hashes = session.query(DocumentPageHash).filter_by(document_id=document_id).all()
    assert len(hashes) == 1

    events = session.query(AuditLog).filter_by(document_id=document_id).all()
    assert {e.event_type for e in events} == {"duplicate_check_completed"}


def test_second_identical_upload_flags(task_session_factory, monkeypatch):
    pdf_bytes = _minimal_pdf_bytes()
    first_id = _seed_document(task_session_factory, filename="a.pdf")
    _patch_storage(monkeypatch, pdf_bytes)
    duplicate_check_task_module.run_duplicate_check_task(str(first_id))

    second_id = _seed_document(task_session_factory, filename="a_copy.pdf")
    _patch_storage(monkeypatch, pdf_bytes)
    duplicate_check_task_module.run_duplicate_check_task(str(second_id))

    session = task_session_factory()
    check = session.query(DocumentCheck).filter_by(document_id=second_id).one()
    assert check.status == DocumentCheckStatus.completed
    assert check.result["result"] == "flag"
    finding = check.result["details"][0]
    assert finding["finding"] == "near_duplicate_page"
    assert finding["data"]["matched_document_id"] == str(first_id)
    assert finding["data"]["matched_document_filename"] == "a.pdf"
    assert finding["data"]["distance"] == 0
    assert "a.pdf" in finding["description"]


def test_non_pdf_document_is_a_quiet_noop(task_session_factory, monkeypatch):
    document_id = _seed_document(task_session_factory, content_type="image/png", filename="scan.png")
    fake_storage = _patch_storage(monkeypatch, b"not used")

    duplicate_check_task_module.run_duplicate_check_task(str(document_id))

    fake_storage.download_bytes.assert_not_called()
    session = task_session_factory()
    assert session.query(DocumentCheck).count() == 0
    assert session.query(DocumentPageHash).count() == 0


def test_noop_for_unknown_document_id(task_session_factory):
    duplicate_check_task_module.run_duplicate_check_task(str(uuid.uuid4()))
    session = task_session_factory()
    assert session.query(DocumentCheck).count() == 0


def test_storage_failure_marks_check_failed_not_worker_crash(task_session_factory, monkeypatch):
    document_id = _seed_document(task_session_factory)
    fake_storage = MagicMock()
    fake_storage.download_bytes.side_effect = RuntimeError("simulated blob outage")
    monkeypatch.setattr(duplicate_check_task_module, "get_storage_service_for_task", lambda: fake_storage)

    duplicate_check_task_module.run_duplicate_check_task(str(document_id))

    session = task_session_factory()
    check = session.query(DocumentCheck).filter_by(document_id=document_id).one()
    assert check.status == DocumentCheckStatus.failed
    assert "simulated blob outage" in check.error_message
    assert session.query(DocumentPageHash).filter_by(document_id=document_id).count() == 0

    events = session.query(AuditLog).filter_by(document_id=document_id).all()
    assert {e.event_type for e in events} == {"duplicate_check_failed"}
