"""
Fair-share priorities (app/tasks/fairshare.py) against the real broker Redis:
a company's tasks drop one priority level per bucket of outstanding work, a
newcomer gets the top priority, and finishing a task frees its slot.
Publishing itself is intercepted — nothing reaches a real queue.
"""
import uuid

import pytest
from celery import Task

from app.tasks import fairshare
from app.tasks.document_processing import process_document
from app.tasks.metadata_forensics_task import run_metadata_forensics


def _redis_ok():
    try:
        fairshare._redis().ping()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _redis_ok(), reason="broker Redis not reachable")


@pytest.fixture()
def published(monkeypatch):
    """Capture what would be published instead of sending it."""
    sent = []

    def fake_super_apply_async(self, args=None, kwargs=None, **options):
        sent.append({"task": self.name, "args": args, "priority": options.get("priority"),
                     "headers": options.get("headers") or {}})

    monkeypatch.setattr(Task, "apply_async", fake_super_apply_async)
    yield sent
    for key in fairshare._redis().scan_iter("fddt:fairshare:*"):
        fairshare._redis().delete(key)


def test_big_batch_sinks_and_a_newcomer_goes_first(published, monkeypatch):
    monkeypatch.setattr(fairshare, "bucket_size", lambda: 5)
    company_a, company_b = str(uuid.uuid4()), str(uuid.uuid4())
    for _ in range(23):
        FairApply(process_document, [str(uuid.uuid4()), company_a])
    FairApply(process_document, [str(uuid.uuid4()), company_b])

    a_priorities = [p["priority"] for p in published[:23]]
    assert a_priorities[:5] == [0] * 5 and a_priorities[5:10] == [1] * 5 and a_priorities[-1] == 4
    assert published[-1]["priority"] == 0  # B jumps ahead of A's tail
    assert published[-1]["headers"][fairshare.COMPANY_HEADER] == company_b
    assert published[-1]["headers"][fairshare.QUEUE_HEADER] == "extraction_queue"
    assert fairshare.outstanding("extraction_queue") == {company_a: 23, company_b: 1}


def test_priorities_are_per_queue_and_recover_as_work_finishes(published, monkeypatch):
    monkeypatch.setattr(fairshare, "bucket_size", lambda: 2)
    company = str(uuid.uuid4())
    for _ in range(6):
        FairApply(process_document, [str(uuid.uuid4()), company])
    # Another queue is counted separately.
    FairApply(run_metadata_forensics, [str(uuid.uuid4()), company])
    assert published[-1]["priority"] == 0
    for _ in range(6):
        fairshare.task_finished("extraction_queue", company)
    assert fairshare.outstanding("extraction_queue") == {}
    FairApply(process_document, [str(uuid.uuid4()), company])
    assert published[-1]["priority"] == 0  # back at the front once its backlog drained


def test_tasks_without_a_company_are_published_plainly(published):
    FairApply(process_document, [str(uuid.uuid4())])
    assert published[-1]["priority"] is None


def test_idle_queue_resets_counters(published):
    company = str(uuid.uuid4())
    FairApply(process_document, [str(uuid.uuid4()), company])
    fairshare.reset_if_idle("extraction_queue", idle=True)
    assert fairshare.outstanding("extraction_queue") == {}


def FairApply(task, args):  # noqa: N802 - reads like the call it stands for
    fairshare.FairShareTask.apply_async(task, args)
