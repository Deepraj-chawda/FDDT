"""
Task retries never duplicate rows (task_acks_late redelivery).

Two crash shapes are simulated:

* Crash AFTER commit, before the ack: Celery redelivers and the task runs a
  second time in full. → still exactly one check row / one row per page hash.
* Crash MID-task (the worker dies before committing): nothing from the first
  attempt is persisted; the redelivered run writes the row once.

Also: a failed attempt followed by a successful retry leaves ONE row, holding
the successful result.
"""
import io
from unittest.mock import MagicMock

import pikepdf
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.pool import StaticPool

import app.tasks.duplicate_check_task as duplicate_task
import app.tasks.metadata_forensics_task as metadata_task
from app.models import Base
from app.models.case import Case, CaseStatus, CaseType
from app.models.document import Document
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType
from app.models.document_page_hash import DocumentPageHash
from app.models.user import User, UserRole
from tests.conftest import tenant_task_factory


class WorkerKilled(BaseException):
    """Stands in for the worker process dying (not a normal Exception, so the
    task's own error handling doesn't catch it — like SIGKILL)."""


def _pdf(pages=2) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72 + i * 20), f"Invoice page {i + 1} — total 1,250.00", fontsize=14)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture()
def factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    f = tenant_task_factory(monkeypatch, engine)
    for module in (metadata_task, duplicate_task):
        monkeypatch.setattr(module, "SessionLocal", f)
        monkeypatch.setattr(module, "request_case_scoring", lambda *a, **k: None)
    storage = MagicMock()
    storage.download_bytes.return_value = _pdf()
    for module in (metadata_task, duplicate_task):
        monkeypatch.setattr(module, "get_storage_service_for_task", lambda: storage)
    return f


def _document(factory) -> str:
    s = factory()
    try:
        user = User(email="u@example.com", hashed_password="x", role=UserRole.user, is_active=True)
        s.add(user)
        s.flush()
        case = Case(case_number="CASE-IDEMP01", case_type=CaseType.other, submitted_by_user_id=user.id,
                    status=CaseStatus.submitted)
        s.add(case)
        s.flush()
        doc = Document(case_id=case.id, uploaded_by_user_id=user.id, original_filename="inv.pdf",
                       blob_storage_path="https://fake/documents/inv.pdf", file_hash="0" * 64,
                       content_type="application/pdf")
        s.add(doc)
        s.commit()
        return str(doc.id)
    finally:
        s.close()


def _count(factory, model, **where):
    s = factory()
    try:
        stmt = select(func.count()).select_from(model)
        for k, v in where.items():
            stmt = stmt.where(getattr(model, k) == v)
        return s.execute(stmt).scalar_one()
    finally:
        s.close()


def test_redelivered_task_after_commit_keeps_one_check_row(factory):
    doc = _document(factory)
    metadata_task.run_metadata_forensics(doc)
    metadata_task.run_metadata_forensics(doc)  # the redelivery
    metadata_task.run_metadata_forensics(doc)
    import uuid as _u

    assert _count(factory, DocumentCheck, document_id=_u.UUID(doc), check_type=DocumentCheckType.metadata_forensics) == 1


def test_worker_killed_mid_task_then_redelivered_writes_one_row(factory, monkeypatch):
    import uuid as _u

    doc = _document(factory)
    real_record = metadata_task.record_event

    def die_once(*args, **kwargs):
        monkeypatch.setattr(metadata_task, "record_event", real_record)
        raise WorkerKilled()

    monkeypatch.setattr(metadata_task, "record_event", die_once)
    with pytest.raises(WorkerKilled):
        metadata_task.run_metadata_forensics(doc)  # dies after staging the check, before commit
    assert _count(factory, DocumentCheck, document_id=_u.UUID(doc)) == 0

    metadata_task.run_metadata_forensics(doc)  # redelivered
    assert _count(factory, DocumentCheck, document_id=_u.UUID(doc), check_type=DocumentCheckType.metadata_forensics) == 1


def test_failed_attempt_then_successful_retry_leaves_the_success(factory, monkeypatch):
    import uuid as _u

    doc = _document(factory)
    real_analyze = metadata_task.analyze_pdf_metadata
    calls = {"n": 0}

    def flaky(pdf_bytes):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient storage hiccup")
        return real_analyze(pdf_bytes)

    monkeypatch.setattr(metadata_task, "analyze_pdf_metadata", flaky)
    metadata_task.run_metadata_forensics(doc)  # first attempt: recorded as failed
    metadata_task.run_metadata_forensics(doc)  # retry succeeds

    s = factory()
    try:
        rows = s.execute(select(DocumentCheck).where(DocumentCheck.document_id == _u.UUID(doc))).scalars().all()
    finally:
        s.close()
    assert len(rows) == 1 and rows[0].status == DocumentCheckStatus.completed and rows[0].error_message is None


def test_redelivered_duplicate_check_keeps_one_hash_per_page(factory):
    import uuid as _u

    doc = _document(factory)
    duplicate_task.run_duplicate_check_task(doc)
    duplicate_task.run_duplicate_check_task(doc)
    assert _count(factory, DocumentPageHash, document_id=_u.UUID(doc)) == 2  # 2 pages, not 4
    assert _count(factory, DocumentCheck, document_id=_u.UUID(doc), check_type=DocumentCheckType.duplicate_detection) == 1
    # ...and the re-run did not flag the document as a duplicate of itself.
    s = factory()
    try:
        check = s.execute(select(DocumentCheck).where(DocumentCheck.document_id == _u.UUID(doc))).scalar_one()
    finally:
        s.close()
    assert check.result["result"] == "pass"
