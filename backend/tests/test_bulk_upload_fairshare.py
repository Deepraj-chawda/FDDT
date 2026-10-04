"""Bulk upload under fair-share scheduling (app/tasks/fairshare.py), against
the real broker Redis (skipped without it). Publishing is intercepted, so
nothing reaches a real queue.

Company A bulk uploads a 100-case zip; seconds later Company B uploads one
document. B's tasks must not wait behind all of A's: on every queue, B's
document gets the top priority, while A's tasks sink one priority level per
bucket of its own outstanding work. Workers drain priority 0 first, so B is
served after at most one bucket of A's tasks per queue, not after A's 100."""
import uuid

import pytest
from celery import Task
from sqlalchemy import create_engine, event, select
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.models.bulk_upload import BulkUpload
from app.models.case import CaseType
from app.models.document import Document
from app.models.user import User, UserRole
from app.services import bulk_upload_service, document_intake
from app.services.bulk_upload_service import inspect_zip
from app.tasks import fairshare
from app.tasks.celery_app import TASK_QUEUES
from app.tasks.document_processing import process_document
from app.tasks.duplicate_check_task import run_duplicate_check_task
from app.tasks.metadata_forensics_task import run_metadata_forensics
from app.tasks.signature_detection_task import run_signature_detection
from app.tasks.tampering_checks_task import run_tampering_checks
from app.tasks.visual_inconsistency_task import run_visual_inconsistency_review_task
from tests.conftest import FakeStorageService, tenant_task_factory
from tests.sample_files import make_pdf, make_zip

PIPELINE_TASKS = (
    process_document, run_metadata_forensics, run_tampering_checks, run_duplicate_check_task,
    run_visual_inconsistency_review_task, run_signature_detection,
)


def _redis_ok():
    try:
        fairshare._redis().ping()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _redis_ok(), reason="broker Redis not reachable")


@pytest.fixture()
def published(monkeypatch):
    # The suite no-ops every task's .delay (conftest._no_celery); put the real
    # one back so publishes go through FairShareTask.apply_async.
    for task in PIPELINE_TASKS:
        monkeypatch.delattr(task, "delay")
    sent = []

    def fake_publish(self, args=None, kwargs=None, **options):
        sent.append({"task": self.name, "company": args[1], "priority": options.get("priority")})

    monkeypatch.setattr(Task, "apply_async", fake_publish)
    for key in fairshare._redis().scan_iter("fddt:fairshare:*"):
        fairshare._redis().delete(key)
    yield sent
    for key in fairshare._redis().scan_iter("fddt:fairshare:*"):
        fairshare._redis().delete(key)


def test_another_companys_single_upload_is_not_blocked_behind_a_100_case_zip(monkeypatch, published):
    import io

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    event.listen(engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    factory = tenant_task_factory(monkeypatch, engine)
    company_a = factory.company_id
    db = factory()
    storage = FakeStorageService()

    uploader = User(email="bulk@a.example", hashed_password="x", role=UserRole.user, is_active=True)
    db.add(uploader)
    db.commit()
    data = make_zip({f"case-{i:03d}/claim.pdf": make_pdf(f"claim {i}") for i in range(100)})
    plan = inspect_zip(io.BytesIO(data), len(data), max_zip_bytes=300 * 1024 * 1024,
                       max_file_bytes=10 * 1024 * 1024).plan
    bulk = BulkUpload(
        uploaded_by_user_id=uploader.id, original_filename="a.zip", case_type=CaseType.vendor_invoice,
        blob_storage_path=storage.upload("companies/a/bulk.zip", data), file_hash="0" * 64,
        zip_size_bytes=len(data), case_folder_count=100,
    )
    db.add(bulk)
    db.flush()
    db.add_all(bulk_upload_service.plan_rows(bulk, plan))
    db.commit()

    bulk_upload_service.ingest(db, storage, bulk.id, company_a)  # real enqueue_document_pipeline
    assert len(published) == 100 * len(PIPELINE_TASKS)

    # Company B, seconds later: one ordinary single upload.
    company_b = uuid.uuid4()
    document_intake.enqueue_document_pipeline(uuid.uuid4(), company_b)

    a_tasks = [p for p in published if p["company"] == str(company_a)]
    b_tasks = [p for p in published if p["company"] == str(company_b)]
    assert len(b_tasks) == len(PIPELINE_TASKS)
    assert {p["priority"] for p in b_tasks} == {0}, "B's document goes to the front of every queue"

    bucket = fairshare.bucket_size()
    for task in PIPELINE_TASKS:
        a_priorities = [p["priority"] for p in a_tasks if p["task"] == task.name]
        ahead_of_b = sum(1 for p in a_priorities if p == 0)
        assert ahead_of_b <= bucket, f"{task.name}: B waits behind at most one bucket of A's tasks"
        assert max(a_priorities) == fairshare.MAX_PRIORITY  # A's tail sank to the back
    # The ingestion itself is fair-share too: its own task carries the company.
    assert TASK_QUEUES["ingest_bulk_upload"] == "forensics_queue"
    assert db.execute(select(Document)).scalars().all().__len__() == 100
    db.close()
