"""
Unit tests for the metadata-forensics Celery task
(app/tasks/metadata_forensics_task.py). Same in-memory-SQLite pattern as
tests/test_document_processing.py — monkeypatches the task module's
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

import app.tasks.metadata_forensics_task as metadata_forensics_task_module
from app.models import Base
from app.models.audit_log import AuditLog
from app.models.case import Case, CaseStatus, CaseType
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.models.user import User, UserRole


def _minimal_pdf_bytes(producer: str | None = None) -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page()
    if producer:
        pdf.docinfo["/Producer"] = producer
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
    monkeypatch.setattr(metadata_forensics_task_module, "SessionLocal", factory)
    return factory


def _seed_document(session_factory, *, content_type="application/pdf", filename="invoice.pdf"):
    session = session_factory()
    try:
        user = User(email="uploader@example.com", hashed_password="x", role=UserRole.user, is_active=True)
        session.add(user)
        session.flush()
        case = Case(
            case_number="CASE-MDFORENSICS",
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
    monkeypatch.setattr(metadata_forensics_task_module, "get_storage_service_for_task", lambda: fake_storage)
    return fake_storage


def test_writes_passing_check_for_clean_pdf(task_session_factory, monkeypatch):
    document_id = _seed_document(task_session_factory)
    _patch_storage(monkeypatch, _minimal_pdf_bytes(producer="ReportLab"))

    metadata_forensics_task_module.run_metadata_forensics(str(document_id))

    session = task_session_factory()
    check = (
        session.query(DocumentCheck)
        .filter_by(document_id=document_id, check_type=DocumentCheckType.metadata_forensics)
        .one()
    )
    assert check.status == DocumentCheckStatus.completed
    assert check.result["result"] == "pass"
    assert isinstance(check.result["details"], list)

    events = session.query(AuditLog).filter_by(document_id=document_id).all()
    assert {e.event_type for e in events} == {"metadata_forensics_completed"}


def test_writes_flagged_check_for_tampered_pdf(task_session_factory, monkeypatch):
    document_id = _seed_document(task_session_factory)
    _patch_storage(monkeypatch, _minimal_pdf_bytes(producer="Adobe Photoshop 2024"))

    metadata_forensics_task_module.run_metadata_forensics(str(document_id))

    session = task_session_factory()
    check = (
        session.query(DocumentCheck)
        .filter_by(document_id=document_id, check_type=DocumentCheckType.metadata_forensics)
        .one()
    )
    assert check.result["result"] == "flag"
    assert any(f["finding"] == "editing_software_detected" for f in check.result["details"])


def test_non_pdf_document_is_a_quiet_noop(task_session_factory, monkeypatch):
    document_id = _seed_document(task_session_factory, content_type="image/png", filename="scan.png")
    fake_storage = _patch_storage(monkeypatch, b"not used")

    metadata_forensics_task_module.run_metadata_forensics(str(document_id))

    fake_storage.download_bytes.assert_not_called()
    session = task_session_factory()
    assert session.query(DocumentCheck).count() == 0


def test_pdf_detected_via_filename_when_content_type_missing(task_session_factory, monkeypatch):
    document_id = _seed_document(task_session_factory, content_type=None, filename="invoice.PDF")
    _patch_storage(monkeypatch, _minimal_pdf_bytes())

    metadata_forensics_task_module.run_metadata_forensics(str(document_id))

    session = task_session_factory()
    assert session.query(DocumentCheck).filter_by(document_id=document_id).count() == 1


def test_noop_for_unknown_document_id(task_session_factory):
    # Doesn't raise.
    metadata_forensics_task_module.run_metadata_forensics(str(uuid.uuid4()))
    session = task_session_factory()
    assert session.query(DocumentCheck).count() == 0


def test_storage_failure_marks_check_failed_not_worker_crash(task_session_factory, monkeypatch):
    document_id = _seed_document(task_session_factory)
    fake_storage = MagicMock()
    fake_storage.download_bytes.side_effect = RuntimeError("simulated blob outage")
    monkeypatch.setattr(metadata_forensics_task_module, "get_storage_service_for_task", lambda: fake_storage)

    metadata_forensics_task_module.run_metadata_forensics(str(document_id))

    session = task_session_factory()
    check = (
        session.query(DocumentCheck)
        .filter_by(document_id=document_id, check_type=DocumentCheckType.metadata_forensics)
        .one()
    )
    assert check.status == DocumentCheckStatus.failed
    assert "simulated blob outage" in check.error_message

    events = session.query(AuditLog).filter_by(document_id=document_id).all()
    assert {e.event_type for e in events} == {"metadata_forensics_failed"}
