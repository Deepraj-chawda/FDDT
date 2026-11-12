"""Recovery of work lost by a crashed worker (app/tasks/task_recovery.py) and
the memory budget that keeps a worker from being OOM-killed by large PDFs
(app/services/forensics/pdf_render.py)."""
import json
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

import pymupdf
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.tasks.task_recovery as recovery
from app.core.config import settings
from app.db import tenancy
from app.models import Base
from app.models.audit_log import AuditLog
from app.models.base import utcnow
from app.models.case import Case, CaseStatus, CaseType
from app.models.document import Document, DocumentProcessingStatus
from app.models.document_check import DocumentCheck, DocumentCheckStatus, DocumentCheckType as T
from app.models.user import User, UserRole
from app.services.forensics import pdf_render
from app.tasks.celery_app import celery_app
from tests.conftest import tenant_task_factory

ALL_PDF_CHECKS = (
    T.field_validation, T.issuer_verification, T.metadata_forensics, T.error_level_analysis,
    T.copy_move_detection, T.duplicate_detection, T.visual_inconsistency_review, T.signature_stamp_detection,
)


# ---------------------------------------------------------------------------
# Missing checks on finished documents
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = tenant_task_factory(monkeypatch, engine)
    maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    @contextmanager
    def _system_session():
        session = tenancy.bind_platform(maker())
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(recovery, "SessionLocal", factory)
    monkeypatch.setattr(recovery, "system_session", _system_session)
    busy: dict[str, bool | None] = {}
    monkeypatch.setattr("app.services.queue_monitor.queue_busy", lambda q: busy.get(q, False))
    queued: list[tuple[str, str]] = []
    for name in set(recovery._TASK_FOR_CHECK.values()):
        monkeypatch.setattr(celery_app.tasks[name], "delay", lambda doc, co, _n=name: queued.append((_n, doc)))
    monkeypatch.setattr("app.services.risk_scoring_service.request_case_scoring", lambda *a: None)
    monkeypatch.setattr("app.tasks.document_checks._maybe_enqueue_cross_document_check", lambda *a: None)
    monkeypatch.setattr(settings, "stuck_check_minutes", 15)
    monkeypatch.setattr(settings, "stuck_document_max_requeues", 3)

    session = factory()
    user = User(email="u@example.com", hashed_password="x", role=UserRole.user, is_active=True)
    session.add(user)
    session.flush()
    case = Case(case_number="CASE-RECOV1", case_type=CaseType.vendor_invoice,
                submitted_by_user_id=user.id, status=CaseStatus.submitted)
    session.add(case)
    session.commit()

    def add_document(age_minutes, done=ALL_PDF_CHECKS, status=DocumentProcessingStatus.complete, name="a.pdf"):
        when = utcnow() - timedelta(minutes=age_minutes)
        doc = Document(case_id=case.id, uploaded_by_user_id=user.id, original_filename=name,
                       blob_storage_path=f"https://x/{name}", file_hash="0" * 64,
                       content_type="application/pdf" if name.endswith(".pdf") else "image/png",
                       file_size_bytes=1, processing_status=status, created_at=when, updated_at=when)
        session.add(doc)
        session.flush()
        for check_type in done:
            session.add(DocumentCheck(company_id=doc.company_id, document_id=doc.id, check_type=check_type,
                                      status=DocumentCheckStatus.completed, result={}))
        session.commit()
        return doc.id

    def checks(doc_id):
        with _system_session() as db:
            return {c.check_type: c for c in db.execute(
                select(DocumentCheck).where(DocumentCheck.document_id == doc_id)).scalars()}

    def audit(doc_id, event_type):
        with _system_session() as db:
            return list(db.execute(select(AuditLog).where(
                AuditLog.document_id == doc_id, AuditLog.event_type == event_type)).scalars())

    yield type("Env", (), {"add": staticmethod(add_document), "queued": queued, "busy": busy,
                           "checks": staticmethod(checks), "audit": staticmethod(audit)})
    session.close()


def _without(*missing):
    return tuple(c for c in ALL_PDF_CHECKS if c not in missing)


def test_requeues_only_the_lost_checks_task_once_the_queue_is_idle(env):
    """The incident: an OOM kill lost one document's visual review and
    signature detection; every other check finished."""
    lost = env.add(30, done=_without(T.visual_inconsistency_review, T.signature_stamp_detection))
    complete = env.add(30)

    assert recovery.requeue_missing_checks() == {"requeued": 1, "marked_failed": 0}
    assert sorted(env.queued) == [("run_signature_detection", str(lost)),
                                  ("run_visual_inconsistency_review", str(lost))]
    assert str(complete) not in {doc for _, doc in env.queued}
    (row,) = env.audit(lost, recovery.CHECKS_REQUEUED_EVENT)
    assert row.event_data["attempt"] == 1
    assert row.event_data["checks"] == ["visual_inconsistency_review", "signature_stamp_detection"]


def test_one_task_is_queued_once_for_all_the_checks_it_writes(env):
    env.add(30, done=_without(T.error_level_analysis, T.copy_move_detection, T.field_validation, T.issuer_verification))
    recovery.requeue_missing_checks()
    assert sorted(name for name, _ in env.queued) == ["run_document_checks", "run_tampering_checks"]


def test_waits_while_the_documents_were_recent_or_the_queue_is_busy(env):
    env.add(5, done=_without(T.visual_inconsistency_review))  # finished extraction 5 min ago
    assert recovery.requeue_missing_checks() == {"requeued": 0, "marked_failed": 0}

    env.add(30, done=_without(T.visual_inconsistency_review))
    env.busy["vision_queue"] = True  # a backlog: the task may still be coming
    assert recovery.requeue_missing_checks() == {"requeued": 0, "marked_failed": 0}
    env.busy["vision_queue"] = None  # broker unreachable: can't tell
    assert recovery.requeue_missing_checks() == {"requeued": 0, "marked_failed": 0}
    assert env.queued == []


def test_extraction_checks_only_expected_after_a_successful_extraction_and_pdf_checks_only_for_pdfs(env):
    env.add(30, status=DocumentProcessingStatus.failed, done=_without(T.field_validation, T.issuer_verification))
    env.add(30, name="scan.png", done=(T.field_validation, T.issuer_verification))
    assert recovery.requeue_missing_checks() == {"requeued": 0, "marked_failed": 0}


def test_gives_up_after_max_requeues_and_records_the_checks_as_failed(env):
    doc = env.add(30, done=_without(T.visual_inconsistency_review))
    for _ in range(3):
        assert recovery.requeue_missing_checks()["requeued"] == 1
    assert recovery.requeue_missing_checks() == {"requeued": 0, "marked_failed": 1}

    check = env.checks(doc)[T.visual_inconsistency_review]
    assert check.status == DocumentCheckStatus.failed and "3 automatic retries" in check.error_message
    (row,) = env.audit(doc, "document_checks_failed")
    assert row.event_data["reason"] == "stuck_check_max_requeues"
    assert recovery.requeue_missing_checks() == {"requeued": 0, "marked_failed": 0}  # nothing left to do


def test_zero_minutes_disables_it(env, monkeypatch):
    env.add(600, done=_without(T.visual_inconsistency_review))
    monkeypatch.setattr(settings, "stuck_check_minutes", 0)
    assert recovery.requeue_missing_checks() == {"skipped": "disabled"}


# ---------------------------------------------------------------------------
# Orphaned unacknowledged tasks
# ---------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self, unacked, index):
        self.unacked, self.index = unacked, index

    def hgetall(self, _key):
        return dict(self.unacked)

    def zscore(self, _key, tag):
        return self.index.get(tag)


class _FakeQoS:
    unacked_key, unacked_index_key = "unacked", "unacked_index"

    def __init__(self):
        self.restored = []

    def restore_by_tag(self, tag):
        self.restored.append(tag)


@pytest.fixture()
def broker(monkeypatch):
    now = time.time()
    unacked, index = {}, {}
    qos = _FakeQoS()
    channel = type("Channel", (), {"qos": qos, "client": _FakeRedis(unacked, index)})()

    @contextmanager
    def connection_for_write():
        yield type("Conn", (), {"default_channel": channel})()

    monkeypatch.setattr(celery_app, "connection_for_write", connection_for_write)
    monkeypatch.setattr(settings, "orphaned_task_min_age_seconds", 300)
    live = {"ids": set(), "queues": {"vision_queue", "forensics_queue", "extraction_queue"}}
    monkeypatch.setattr(recovery, "_live_workers", lambda: (live["ids"], live["queues"]) if live is not None else None)

    def add(tag, task_id, queue, age):
        message = {"headers": {"id": task_id, "task": "run_visual_inconsistency_review"}, "properties": {}}
        unacked[tag] = json.dumps([message, "", queue])
        index[tag] = now - age

    return type("Broker", (), {"add": staticmethod(add), "qos": qos, "live": live})


def test_restores_an_old_unacked_task_no_live_worker_holds(broker):
    broker.add("tag-orphan", "t1", "vision_queue", age=600)        # its worker was OOM-killed
    broker.add("tag-running", "t2", "vision_queue", age=600)       # long task, worker alive
    broker.add("tag-fresh", "t3", "vision_queue", age=30)          # just delivered
    broker.add("tag-unknown-queue", "t4", "bulk_queue", age=600)   # no worker of that queue replied
    broker.live["ids"].add("t2")

    assert recovery.restore_orphaned_tasks() == {"restored": 1}
    assert broker.qos.restored == ["tag-orphan"]


def test_does_nothing_when_no_worker_replies_or_when_disabled(broker, monkeypatch):
    broker.add("tag-orphan", "t1", "vision_queue", age=600)
    monkeypatch.setattr(recovery, "_live_workers", lambda: None)
    assert recovery.restore_orphaned_tasks() == {"skipped": "no_worker_replied"}
    monkeypatch.setattr(settings, "orphaned_task_min_age_seconds", 0)
    assert recovery.restore_orphaned_tasks() == {"skipped": "disabled"}
    assert broker.qos.restored == []


def test_the_housekeeping_job_runs_every_step_even_if_one_fails(monkeypatch):
    import app.tasks.stuck_documents_task as stuck

    calls = []
    monkeypatch.setattr(recovery, "restore_orphaned_tasks", lambda: (_ for _ in ()).throw(RuntimeError("redis down")))
    monkeypatch.setattr(stuck, "requeue_stuck", lambda: calls.append("documents") or {"requeued": 0})
    monkeypatch.setattr(recovery, "requeue_missing_checks", lambda: calls.append("checks") or {"requeued": 0})
    result = stuck.requeue_stuck_documents()
    assert result["orphaned_tasks"] == {"error": True} and calls == ["documents", "checks"]


# ---------------------------------------------------------------------------
# Memory budget for rendered pages
# ---------------------------------------------------------------------------

def _pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page(width=595, height=842)  # A4
    return doc.tobytes()


@pytest.fixture()
def budget(monkeypatch):
    monkeypatch.setattr(pdf_render, "_budget", None)
    yield
    monkeypatch.setattr(pdf_render, "_budget", None)


def test_no_budget_outside_a_worker_or_without_a_memory_limit(budget, monkeypatch):
    assert pdf_render._budget is None
    assert len(pdf_render.render_pdf_pages(_pdf(2))) == 2  # API / tests: unchanged behaviour
    monkeypatch.setattr(pdf_render, "_container_memory_limit", lambda: None)
    monkeypatch.delenv("RENDER_MEMORY_BUDGET_MB", raising=False)
    assert pdf_render.enable_render_budget() is None


def test_budget_is_half_the_container_limit(budget, monkeypatch):
    monkeypatch.delenv("RENDER_MEMORY_BUDGET_MB", raising=False)
    monkeypatch.setattr(pdf_render, "_container_memory_limit", lambda: 2 << 30)
    assert pdf_render.enable_render_budget() == 1 << 30
    monkeypatch.setenv("RENDER_MEMORY_BUDGET_MB", "300")
    assert pdf_render.enable_render_budget() == 300 << 20


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="prefork pools exist on Linux only")
def test_prefork_processes_share_one_budget_and_a_dead_process_frees_its_share(budget, monkeypatch):
    import multiprocessing
    import os

    page_bytes = pdf_render.estimated_render_bytes(pymupdf.open(stream=_pdf(1), filetype="pdf"))
    monkeypatch.setenv("RENDER_MEMORY_BUDGET_MB", str((3 * page_bytes >> 20) + 1))  # room for 3 pages
    pdf_render.enable_render_budget(shared_processes=2)
    ctx = multiprocessing.get_context("fork")

    def hold_two_pages_then_die(ready):
        pdf_render.render_pdf_pages(_pdf(2))
        ready.set()
        os._exit(137)  # killed mid-task: never releases

    ready = ctx.Event()
    child = ctx.Process(target=hold_two_pages_then_die, args=(ready,))
    child.start()
    assert ready.wait(10)
    child.join(10)

    # The dead child's 2 pages no longer count: 2 more pages fit in the 3-page budget.
    started = time.monotonic()
    pdf_render.render_pdf_pages(_pdf(2))
    assert time.monotonic() - started < 2
    pdf_render.release_render_budget()
    assert pdf_render._budget.in_use == 0


def test_renders_wait_for_budget_instead_of_exceeding_it(budget, monkeypatch):
    page_bytes = pdf_render.estimated_render_bytes(pymupdf.open(stream=_pdf(1), filetype="pdf"))
    monkeypatch.setenv("RENDER_MEMORY_BUDGET_MB", str((3 * page_bytes >> 20) + 1))  # room for 3 pages
    pdf_render.enable_render_budget()
    events = []

    def task(name, pages, hold):
        pdf_render.render_pdf_pages(_pdf(pages))
        events.append(f"{name} rendered")
        time.sleep(hold)
        events.append(f"{name} done")
        pdf_render.release_render_budget()  # what task_postrun does

    first = threading.Thread(target=task, args=("big", 2, 0.5))
    first.start()
    time.sleep(0.2)
    second = threading.Thread(target=task, args=("second", 2, 0))  # 2 + 2 pages > 3: must wait
    second.start()
    first.join()
    second.join()
    assert events.index("big done") < events.index("second rendered")
    assert pdf_render._budget.in_use == 0


def test_a_document_larger_than_the_whole_budget_runs_alone(budget, monkeypatch):
    monkeypatch.setenv("RENDER_MEMORY_BUDGET_MB", "64")  # smaller than a 10-page A4 render
    pdf_render.enable_render_budget()
    assert len(pdf_render.render_pdf_pages(_pdf(10))) == 10  # takes the whole budget, doesn't deadlock
    assert pdf_render._budget.in_use == pdf_render._budget.capacity
    pdf_render.render_pdf_pages(_pdf(1))  # same task rendering again: already holds everything
    pdf_render.release_render_budget()
    assert pdf_render._budget.in_use == 0
