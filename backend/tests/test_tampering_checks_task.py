"""
Unit tests for the image-tampering Celery task
(app/tasks/tampering_checks_task.py). Same in-memory-SQLite pattern as
tests/test_metadata_forensics_task.py — monkeypatches the task module's
`SessionLocal` and calls the task function directly (bypassing
`.delay()`/a real broker), and monkeypatches storage to hand back an
in-memory PDF instead of touching real Azure Blob Storage.
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

import app.tasks.tampering_checks_task as tampering_checks_task_module
from app.models import Base
from app.models.audit_log import AuditLog
from app.models.case import Case, CaseStatus, CaseType
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
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
    monkeypatch.setattr(tampering_checks_task_module, "SessionLocal", factory)
    return factory


def _seed_document(session_factory, *, content_type="application/pdf", filename="invoice.pdf"):
    session = session_factory()
    try:
        user = User(email="uploader@example.com", hashed_password="x", role=UserRole.user, is_active=True)
        session.add(user)
        session.flush()
        case = Case(
            case_number="CASE-TAMPERCHECKS",
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
            blob_storage_path="https://fake.blob.core.windows.net/documents/doc.pdf",
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
    monkeypatch.setattr(tampering_checks_task_module, "get_storage_service_for_task", lambda: fake_storage)
    return fake_storage


def test_writes_both_check_rows_for_clean_pdf(task_session_factory, monkeypatch):
    document_id = _seed_document(task_session_factory)
    _patch_storage(monkeypatch, _minimal_pdf_bytes())

    tampering_checks_task_module.run_tampering_checks(str(document_id))

    session = task_session_factory()
    checks = {
        c.check_type: c
        for c in session.query(DocumentCheck).filter_by(document_id=document_id).all()
    }
    assert set(checks) == {
        DocumentCheckType.error_level_analysis, DocumentCheckType.copy_move_detection, DocumentCheckType.ghost_content,
    }
    # No scan converted to editable text here: nothing for the ghost check to search.
    assert checks[DocumentCheckType.ghost_content].result["result"] == "not_applicable"
    for check in checks.values():
        assert check.status == DocumentCheckStatus.completed
    # A blank page has no embedded raster image at all.
    assert checks[DocumentCheckType.error_level_analysis].result["result"] == "not_applicable"
    assert checks[DocumentCheckType.copy_move_detection].result["result"] == "pass"

    events = session.query(AuditLog).filter_by(document_id=document_id).all()
    assert {e.event_type for e in events} == {"tampering_checks_completed"}


def test_non_pdf_document_is_a_quiet_noop(task_session_factory, monkeypatch):
    document_id = _seed_document(task_session_factory, content_type="image/png", filename="scan.png")
    fake_storage = _patch_storage(monkeypatch, b"not used")

    tampering_checks_task_module.run_tampering_checks(str(document_id))

    fake_storage.download_bytes.assert_not_called()
    session = task_session_factory()
    assert session.query(DocumentCheck).count() == 0


def test_pdf_detected_via_filename_when_content_type_missing(task_session_factory, monkeypatch):
    document_id = _seed_document(task_session_factory, content_type=None, filename="invoice.PDF")
    _patch_storage(monkeypatch, _minimal_pdf_bytes())

    tampering_checks_task_module.run_tampering_checks(str(document_id))

    session = task_session_factory()
    assert session.query(DocumentCheck).filter_by(document_id=document_id).count() == 3


def test_noop_for_unknown_document_id(task_session_factory):
    tampering_checks_task_module.run_tampering_checks(str(uuid.uuid4()))
    session = task_session_factory()
    assert session.query(DocumentCheck).count() == 0


def test_storage_failure_marks_both_checks_failed_not_worker_crash(task_session_factory, monkeypatch):
    document_id = _seed_document(task_session_factory)
    fake_storage = MagicMock()
    fake_storage.download_bytes.side_effect = RuntimeError("simulated blob outage")
    monkeypatch.setattr(tampering_checks_task_module, "get_storage_service_for_task", lambda: fake_storage)

    tampering_checks_task_module.run_tampering_checks(str(document_id))

    session = task_session_factory()
    checks = session.query(DocumentCheck).filter_by(document_id=document_id).all()
    assert len(checks) == 2
    assert {c.check_type for c in checks} == {
        DocumentCheckType.error_level_analysis,
        DocumentCheckType.copy_move_detection,
    }
    for check in checks:
        assert check.status == DocumentCheckStatus.failed
        assert "simulated blob outage" in check.error_message

    events = session.query(AuditLog).filter_by(document_id=document_id).all()
    assert {e.event_type for e in events} == {"tampering_checks_failed"}
