"""
Storing an already-validated document and starting its pipeline — the one
code path behind both the single-file upload endpoint (app/api/documents.py)
and bulk zip ingestion (app/services/bulk_upload_service.py), so a document
that arrives in a zip is stored, recorded, counted and processed exactly
like one uploaded on its own.

Validation itself (size, PDF header bytes, extension, parse, password)
lives in app/services/upload_validation.py and runs BEFORE anything here.

Split in three because of transaction boundaries: `store_original` is slow
(a Blob Storage round-trip) and must run with no DB transaction open;
`record_document` only adds rows to the caller's session (the caller
commits); `enqueue_document_pipeline` runs after that commit, so a worker
never picks up a document id it can't see yet.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath

from sqlalchemy.orm import Session

from app.models.document import Document
from app.services.audit_service import record_event
from app.services.storage_service import StorageService, document_blob_path
from app.services.usage_service import record_document_uploaded

logger = logging.getLogger("fddt.intake")


@dataclass(frozen=True)
class StoredOriginal:
    document_id: uuid.UUID
    file_url: str
    file_hash: str
    size_bytes: int


def store_original(
    storage: StorageService,
    company_id: uuid.UUID,
    case_id: uuid.UUID,
    filename: str | None,
    content: bytes,
    content_type: str,
) -> StoredOriginal:
    """Write the original bytes to Blob Storage, immutably, at a company- and
    case-prefixed path that carries the document id and SHA-256. Raises
    StorageOperationError. Hold no DB transaction while calling this."""
    file_hash = hashlib.sha256(content).hexdigest()
    document_id = uuid.uuid4()
    extension = PurePosixPath(filename or "").suffix  # includes leading "."
    blob_path = document_blob_path(company_id, case_id, document_id, file_hash, extension)
    file_url = storage.upload(blob_path, content, content_type=content_type)
    return StoredOriginal(document_id, file_url, file_hash, len(content))


def record_document(
    db: Session,
    *,
    company_id: uuid.UUID,
    case_id: uuid.UUID,
    uploaded_by_user_id: uuid.UUID,
    filename: str | None,
    content_type: str,
    stored: StoredOriginal,
    audit_extra: dict | None = None,
) -> Document:
    """Add the `documents` row, its `document_uploaded` audit event and the
    usage counter to `db`. The caller commits."""
    document = Document(
        id=stored.document_id,
        company_id=company_id,
        case_id=case_id,
        uploaded_by_user_id=uploaded_by_user_id,
        original_filename=filename or "unnamed",
        blob_storage_path=stored.file_url,
        file_hash=stored.file_hash,
        content_type=content_type,
        file_size_bytes=stored.size_bytes,
    )
    db.add(document)
    # Flush before the audit_log insert so its FK reference to this document
    # is satisfied without relying on an ORM relationship for flush ordering.
    db.flush()
    record_event(
        db,
        "document_uploaded",
        case_id=case_id,
        document_id=document.id,
        actor_user_id=uploaded_by_user_id,
        event_data={
            "original_filename": document.original_filename,
            "file_hash": stored.file_hash,
            "file_size_bytes": stored.size_bytes,
            **(audit_extra or {}),
        },
    )
    record_document_uploaded(db, company_id, stored.size_bytes)
    return document


def enqueue_document_pipeline(document_id: uuid.UUID, company_id: uuid.UUID) -> bool:
    """One independent task per (document, step), each carrying the company
    so the worker opens a session confined to it and the publish gets a
    fair-share priority (app/tasks/fairshare.py). Every step lands on the
    queue for the service it depends on (extraction / vision / forensics —
    app/tasks/celery_app.py); nothing processes a whole case or batch inside
    one task. Call only after the document row has been committed.

    Never raises: the document is already stored and recorded, so a broker
    outage must not turn the upload into an error. Returns False when the
    tasks could not be published; the document then stays `pending` and the
    stuck-document job (app/tasks/stuck_documents_task.py) queues it again
    once the broker is back."""
    try:
        _publish_pipeline(document_id, company_id)
        return True
    except Exception:  # noqa: BLE001 - broker unreachable / publish failed
        logger.exception(
            "could not queue the pipeline for document %s; it stays pending and is retried "
            "by the stuck-document job",
            document_id,
        )
        return False


def _publish_pipeline(document_id: uuid.UUID, company_id: uuid.UUID) -> None:
    # Imported here: the task modules import services, and this module is
    # itself imported by a task (bulk ingestion).
    from app.tasks.document_processing import process_document
    from app.tasks.duplicate_check_task import run_duplicate_check_task
    from app.tasks.metadata_forensics_task import run_metadata_forensics
    from app.tasks.signature_detection_task import run_signature_detection
    from app.tasks.tampering_checks_task import run_tampering_checks
    from app.tasks.visual_inconsistency_task import run_visual_inconsistency_review_task

    args = (str(document_id), str(company_id))
    process_document.delay(*args)
    # All four are independent of extraction (they only read the PDF's own
    # bytes/rendered pages, not OCR text or a classified document_type), so
    # they run in parallel rather than waiting on process_document.
    run_metadata_forensics.delay(*args)
    run_tampering_checks.delay(*args)
    run_duplicate_check_task.delay(*args)
    run_visual_inconsistency_review_task.delay(*args)
    # Detection only (presence/location) — comparison waits for a reviewer
    # to create a reference (app/tasks/signature_comparison_task.py).
    run_signature_detection.delay(*args)
