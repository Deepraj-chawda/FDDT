"""
Celery task: ingest one bulk-upload zip (app/services/bulk_upload_service.py).

Runs on forensics_queue (local CPU: unzip + PDF parsing, no Azure call) and,
like every pipeline task, carries the company as its second argument. So the
ingestion itself gets a fair-share priority, and every document it queues is
an ordinary per-document task with its own fair-share priority. One
company's big zip never holds back another company's work.

The HTTP request only stores the zip and returns. Unzipping, validating and
queueing a 300 MB zip happens here, off the request path.

Storage hiccups are retried (the ingestion resumes where it stopped and never
duplicates a case). Anything else, or a storage error that outlasts the
retries, marks the upload failed with a message. Cases already created stay
created and keep processing.
"""
import logging
import uuid

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.base import utcnow
from app.models.bulk_upload import BulkUpload, BulkUploadStatus
from app.services import bulk_upload_service
from app.services.storage_service import StorageOperationError, get_storage_service_for_task
from app.tasks.celery_app import celery_app
from app.tasks.tenant import open_task_session

logger = logging.getLogger("fddt.bulk_upload")

_MAX_RETRIES = 3


def _mark_failed(bulk_upload_id: uuid.UUID, company_id: uuid.UUID, code: str, message: str) -> None:
    db = open_task_session(SessionLocal, company_id)
    try:
        db.rollback()
        bulk = db.execute(
            select(BulkUpload).where(BulkUpload.id == bulk_upload_id, BulkUpload.company_id == company_id)
        ).scalar_one_or_none()
        if bulk is not None and bulk.status != BulkUploadStatus.complete:
            bulk.status = BulkUploadStatus.failed
            bulk.error_code = code
            bulk.error_message = message
            bulk.finished_at = utcnow()
            db.commit()
    finally:
        db.close()


@celery_app.task(name="ingest_bulk_upload", bind=True, max_retries=_MAX_RETRIES)
def ingest_bulk_upload(self, bulk_upload_id: str, company_id: str) -> None:
    bulk_uuid, company_uuid = uuid.UUID(bulk_upload_id), uuid.UUID(company_id)
    db = open_task_session(SessionLocal, company_uuid)
    try:
        bulk_upload_service.ingest(db, get_storage_service_for_task(), bulk_uuid, company_uuid)
    except StorageOperationError as exc:
        db.rollback()
        if self.request.retries < _MAX_RETRIES:
            raise self.retry(exc=exc, countdown=10 * (self.request.retries + 1))
        logger.exception("bulk upload %s: storage kept failing", bulk_upload_id)
        _mark_failed(
            bulk_uuid, company_uuid, "storage_error",
            "File storage was unavailable while the zip was being processed. Cases already "
            "listed as created are processing; upload the remaining cases again.",
        )
    except Exception:  # noqa: BLE001 - recorded on the row, never silently lost
        db.rollback()
        logger.exception("bulk upload %s: ingestion failed", bulk_upload_id)
        _mark_failed(
            bulk_uuid, company_uuid, "ingestion_error",
            "The zip could not be fully processed. Cases already listed as created are "
            "processing; upload the remaining cases again.",
        )
    finally:
        db.close()
