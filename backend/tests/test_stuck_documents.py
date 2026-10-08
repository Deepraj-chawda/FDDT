"""Stuck-document recovery (app/tasks/stuck_documents_task.py): documents whose
queued tasks were lost (Redis restart/outage) are queued again — only while
the extraction queue is empty and idle, at most N times, then marked failed."""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.tasks.stuck_documents_task as stuck
from app.core.config import settings
from app.db import tenancy
from app.models import Base
from app.models.audit_log import AuditLog
from app.models.base import utcnow
from app.models.case import Case, CaseStatus, CaseType
from app.models.document import Document, DocumentProcessingStatus
from app.models.user import User, UserRole
from app.services import document_intake
from app.tasks.celery_app import HOUSEKEEPING_QUEUE, QUEUES, TASK_QUEUES
from tests.conftest import tenant_task_factory


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = tenant_task_factory(monkeypatch, engine)
    maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    from contextlib import contextmanager

    @contextmanager
    def _system_session():
        session = tenancy.bind_platform(maker())
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(stuck, "SessionLocal", factory)
    monkeypatch.setattr(stuck, "system_session", _system_session)
    monkeypatch.setattr(stuck, "_extraction_queue_busy", lambda: False)
    queued: list[str] = []
    monkeypatch.setattr(stuck, "enqueue_document_pipeline", lambda doc_id, company_id: queued.append(str(doc_id)) or True)
    # Giving up also triggers the case's cross-document check and scoring: not under test here.
    monkeypatch.setattr("app.services.risk_scoring_service.request_case_scoring", lambda *a: None)
    monkeypatch.setattr("app.tasks.document_checks._maybe_enqueue_cross_document_check", lambda *a: None)
    monkeypatch.setattr(settings, "stuck_document_pending_minutes", 10)
    monkeypatch.setattr(settings, "stuck_document_processing_minutes", 30)
    monkeypatch.setattr(settings, "stuck_document_max_requeues", 3)

    session = factory()
    user = User(email="u@example.com", hashed_password="x", role=UserRole.user, is_active=True)
    session.add(user)
    session.flush()
    case = Case(case_number="CASE-STUCK1", case_type=CaseType.vendor_invoice,
                submitted_by_user_id=user.id, status=CaseStatus.submitted)
    session.add(case)
    session.commit()

    def add_document(status, age_minutes):
        when = utcnow() - timedelta(minutes=age_minutes)
        doc = Document(case_id=case.id, uploaded_by_user_id=user.id, original_filename="a.pdf",
                       blob_storage_path="https://x/a.pdf", file_hash="0" * 64, content_type="application/pdf",
                       file_size_bytes=1, processing_status=status, created_at=when, updated_at=when)
        session.add(doc)
        session.commit()
        return doc.id

    def reload(doc_id):
        with _system_session() as db:
            return db.get(Document, doc_id)

    def audit(doc_id, event_type):
        with _system_session() as db:
            return list(db.execute(select(AuditLog).where(
                AuditLog.document_id == doc_id, AuditLog.event_type == event_type)).scalars())

    yield type("Env", (), {"add": staticmethod(add_document), "queued": queued,
                           "reload": staticmethod(reload), "audit": staticmethod(audit)})
    session.close()


def test_requeues_old_pending_and_stale_processing_only(env):
    old_pending = env.add(DocumentProcessingStatus.pending, 11)
    fresh_pending = env.add(DocumentProcessingStatus.pending, 2)
    stale_processing = env.add(DocumentProcessingStatus.processing, 31)
    busy_processing = env.add(DocumentProcessingStatus.processing, 5)
    done = env.add(DocumentProcessingStatus.complete, 600)
    failed = env.add(DocumentProcessingStatus.failed, 600)

    result = stuck.requeue_stuck()

    assert result == {"requeued": 2, "marked_failed": 0}
    assert set(env.queued) == {str(old_pending), str(stale_processing)}
    for untouched in (fresh_pending, busy_processing, done, failed):
        assert str(untouched) not in env.queued
    rows = env.audit(old_pending, stuck.REQUEUE_EVENT)
    assert len(rows) == 1 and rows[0].event_data["attempt"] == 1
    assert rows[0].company_id == env.reload(old_pending).company_id  # visible to the company


def test_does_nothing_while_the_extraction_queue_has_work(env, monkeypatch):
    env.add(DocumentProcessingStatus.pending, 120)
    monkeypatch.setattr(stuck, "_extraction_queue_busy", lambda: True)
    assert stuck.requeue_stuck() == {"skipped": "extraction_queue_busy"}
    assert env.queued == []


def test_does_nothing_when_the_broker_is_unreachable(env, monkeypatch):
    env.add(DocumentProcessingStatus.pending, 120)
    monkeypatch.setattr(stuck, "_extraction_queue_busy", lambda: None)
    assert stuck.requeue_stuck() == {"skipped": "broker_unreachable"}
    assert env.queued == []


def test_a_failed_publish_stops_the_run_without_counting_an_attempt(env, monkeypatch):
    doc = env.add(DocumentProcessingStatus.pending, 60)
    monkeypatch.setattr(stuck, "enqueue_document_pipeline", lambda *a: False)
    assert stuck.requeue_stuck() == {"requeued": 0, "marked_failed": 0}
    assert env.audit(doc, stuck.REQUEUE_EVENT) == []


def test_gives_up_after_max_requeues_and_marks_the_document_failed(env):
    doc = env.add(DocumentProcessingStatus.pending, 60)
    for attempt in range(1, 4):
        assert stuck.requeue_stuck()["requeued"] == 1
        assert len(env.audit(doc, stuck.REQUEUE_EVENT)) == attempt

    assert stuck.requeue_stuck() == {"requeued": 0, "marked_failed": 1}
    document = env.reload(doc)
    assert document.processing_status == DocumentProcessingStatus.failed
    assert "3 automatic retries" in document.processing_error
    failed_rows = env.audit(doc, "document_processing_failed")
    assert len(failed_rows) == 1 and failed_rows[0].event_data["reason"] == "stuck_document_max_requeues"
    assert len(env.queued) == 3


def test_zero_minutes_disables_that_check(env, monkeypatch):
    env.add(DocumentProcessingStatus.pending, 600)
    processing = env.add(DocumentProcessingStatus.processing, 600)
    monkeypatch.setattr(settings, "stuck_document_pending_minutes", 0)
    assert stuck.requeue_stuck()["requeued"] == 1
    assert env.queued == [str(processing)]


def test_enqueue_document_pipeline_never_raises_when_the_broker_is_down(monkeypatch):
    from app.tasks.document_processing import process_document

    def down(*_a, **_k):
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(process_document, "delay", down)
    assert document_intake.enqueue_document_pipeline(uuid.uuid4(), uuid.uuid4()) is False


def test_housekeeping_jobs_run_off_the_processing_queues():
    for task in ("log_queue_metrics", "reconcile_usage_stats", "requeue_stuck_documents"):
        assert TASK_QUEUES[task] == HOUSEKEEPING_QUEUE
    assert HOUSEKEEPING_QUEUE not in QUEUES
