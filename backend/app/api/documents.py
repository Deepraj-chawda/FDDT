"""
Document upload.

One file per request by design (see frontend/src/pages/NewCasePage.tsx):
a case commonly has several supporting documents, and the frontend
calls this endpoint once per selected file, all against the same
case_id, so they share it for cross-document checks later.

After the DB record commits, five independent Celery tasks are
enqueued so the upload response doesn't block on any of their latency:
OCR + classification + extraction (app/tasks/document_processing.py,
which itself chains into field_validation/issuer_verification once it
succeeds — see app/tasks/document_checks.py), PDF metadata forensics
(app/tasks/metadata_forensics_task.py), image-tampering detection — ELA
+ copy-move (app/tasks/tampering_checks_task.py), duplicate/near-
duplicate detection (app/tasks/duplicate_check_task.py), and visual
inconsistency review + AI-generated-content assessment (app/tasks/
visual_inconsistency_task.py). The latter four only read the raw file's
own bytes/rendered pages, not anything extraction produces, so all four
run in parallel rather than waiting. The frontend polls case detail and
shows `processing_status`/each check's status until they settle. Still
out of scope here: cross-document checks and risk scoring — those are
separate, later pipeline stages.
"""
import hashlib
import uuid
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.auth import require_company_role
from app.api.case_access import load_visible_case
from app.api.tenant_access import CaseScope, get_case_scope
from app.core.config import settings
from app.models.document import Document
from app.models.user import User, UserRole
from app.schemas.document import DocumentFileUrlResponse, DocumentResponse
from app.services.audit_service import record_event
from app.services.storage_service import (
    StorageOperationError,
    StorageService,
    document_blob_path,
    ensure_company_blob,
    get_storage_service,
)
from app.services.upload_validation import UploadRejected, check_size, validate_upload
from app.services.usage_service import record_document_uploaded
from app.tasks.document_processing import process_document
from app.tasks.duplicate_check_task import run_duplicate_check_task
from app.tasks.metadata_forensics_task import run_metadata_forensics
from app.tasks.signature_detection_task import run_signature_detection
from app.tasks.tampering_checks_task import run_tampering_checks
from app.tasks.visual_inconsistency_task import run_visual_inconsistency_review_task

router = APIRouter(prefix="/cases", tags=["documents"])


@router.post(
    "/{case_id}/documents",
    response_model=DocumentResponse,
    status_code=201,
    summary="Upload a document to a case",
    description=(
        "One file per request (multipart field `file`). The original bytes are stored immutably "
        "in Blob Storage at `companies/{company_id}/cases/{case_id}/documents/"
        "{document_id}_{sha256}{ext}`, a `documents` row is "
        "written with the file's SHA-256, then the Celery pipeline is enqueued (extraction, "
        "metadata forensics, ELA/copy-move, duplicate detection, visual review, signature "
        "detection). The response returns immediately with `processing_status: pending`; poll "
        "GET /cases/{case_id} for progress. Before anything is stored or queued the file is "
        "validated: at most `MAX_UPLOAD_SIZE_BYTES` (10 MB), not empty, a PDF by its "
        "header bytes (not its name), a matching extension, and locally parseable (not corrupted, "
        "not password-protected). A rejection returns `detail: {code, message, ...}` with one of "
        "the codes `file_empty`, `file_too_large`, `unsupported_file_type`, `file_type_mismatch`, "
        "`file_corrupted`, `file_password_protected`. Each pipeline step is its own task on a shared, "
        "FIFO queue (see app/tasks/celery_app.py). A `user` may upload only to a case they "
        "submitted; reviewers to any case of their company; platform admins never (403)."
    ),
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        400: {"description": "`file_empty`: the file is 0 bytes."},
        403: {"description": "Platform admins have read-only access."},
        404: {"description": "No such case (or, for a `user`, not one they submitted)."},
        413: {"description": "`file_too_large`: over the size limit (`size_bytes`, `max_size_bytes`)."},
        415: {"description": "`unsupported_file_type` (not a PDF by content; images are refused) or "
              "`file_type_mismatch` (extension names a different type than the content)."},
        422: {"description": "`file_corrupted` (cannot be parsed) or `file_password_protected`."},
        502: {"description": "Blob Storage rejected the upload."},
    },
)
async def upload_document(
    case_id: uuid.UUID,
    file: UploadFile,
    current_user: User = Depends(require_company_role(UserRole.user)),
    scope: CaseScope = Depends(get_case_scope),
    storage: StorageService = Depends(get_storage_service),
) -> DocumentResponse:
    # A submitter may only add to their own case; a reviewer to any case of
    # their company.
    db = scope.db
    company_id = scope.company_id
    load_visible_case(db, case_id, current_user)
    # End the read transaction: reading the body and uploading the blob can
    # take seconds, and no DB connection should be held meanwhile.
    db.commit()

    # Validate before anything is stored or queued: a bad file must never
    # reach Blob Storage, a worker, Document Intelligence or Azure OpenAI.
    max_bytes = settings.max_upload_size_bytes
    # Read at most one byte past the limit, so an oversized file is never
    # held in memory whole; its full size comes from the parsed upload.
    content = await file.read(max_bytes + 1)
    try:
        if len(content) > max_bytes:
            check_size(file.size if file.size is not None else len(content), max_bytes)
        # Parsing a 10 MB PDF is CPU work — keep it off the event loop.
        validated = await run_in_threadpool(
            validate_upload, content, file.filename, max_bytes=max_bytes
        )
    except UploadRejected as rejection:
        raise HTTPException(status_code=rejection.status_code, detail=rejection.detail()) from None
    # The type detected from the content, not the browser's declared one
    # (the pipeline's PDF-only gates read this column).
    content_type = validated.content_type

    file_hash = hashlib.sha256(content).hexdigest()
    document_id = uuid.uuid4()
    extension = PurePosixPath(file.filename or "").suffix  # includes leading "."

    # Company- and case-prefixed (storage-layer isolation, per-company
    # storage accounting); the document id + content hash make it unique.
    blob_path = document_blob_path(company_id, case_id, document_id, file_hash, extension)

    try:
        file_url = storage.upload(blob_path, content, content_type=content_type)
    except StorageOperationError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc

    document = Document(
        id=document_id,
        company_id=company_id,
        case_id=case_id,
        uploaded_by_user_id=current_user.id,
        original_filename=file.filename or "unnamed",
        blob_storage_path=file_url,
        file_hash=file_hash,
        content_type=content_type,
        file_size_bytes=len(content),
    )
    db.add(document)
    # See app/api/cases.py: flush before the audit_log insert so its FK
    # reference to this document is satisfied without relying on an ORM
    # relationship for flush ordering.
    db.flush()

    record_event(
        db,
        "document_uploaded",
        case_id=case_id,
        document_id=document_id,
        actor_user_id=current_user.id,
        event_data={
            "original_filename": document.original_filename,
            "file_hash": file_hash,
            "file_size_bytes": document.file_size_bytes,
        },
    )
    record_document_uploaded(db, company_id, len(content))

    db.commit()
    db.refresh(document)

    # One independent task per (document, step), each carrying the company
    # so the worker opens a session confined to it. Every step lands on the
    # queue for the service it depends on (extraction / vision / forensics —
    # app/tasks/celery_app.py), interleaved FIFO with every other company's
    # documents; nothing processes a whole case or batch inside one task.
    args = (str(document.id), str(company_id))
    process_document.delay(*args)
    # All four independent of extraction (only read the PDF's own bytes/
    # rendered pages, not OCR text or a classified document_type) — run
    # in parallel rather than waiting on process_document.
    run_metadata_forensics.delay(*args)
    run_tampering_checks.delay(*args)
    run_duplicate_check_task.delay(*args)
    run_visual_inconsistency_review_task.delay(*args)
    # Detection only (presence/location) — comparison waits for a reviewer
    # to create a reference (app/tasks/signature_comparison_task.py).
    run_signature_detection.delay(*args)

    try:
        signed_url = storage.get_download_url(document.blob_storage_path)
    except StorageOperationError:
        signed_url = None  # fall back to the durable URL; upload itself succeeded

    return DocumentResponse.from_document(document, file_url=signed_url)


@router.get(
    "/{case_id}/documents/{document_id}/file-url",
    response_model=DocumentFileUrlResponse,
    summary="Get a fresh signed URL for a document",
    responses={
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        404: {"description": "No such document in this case (or, for a `user`, not their case)."},
        502: {"description": "Blob Storage failed to sign the URL."},
    },
)
def get_document_file_url(
    case_id: uuid.UUID,
    document_id: uuid.UUID,
    scope: CaseScope = Depends(get_case_scope),
    storage: StorageService = Depends(get_storage_service),
) -> DocumentFileUrlResponse:
    """Mint a fresh signed URL for a document's original file — the URL
    handed back at upload time expires after a few minutes, which a
    reviewer working through the post-upload screen can easily outlast."""
    db = scope.db
    load_visible_case(db, case_id, scope.ctx.user)
    document = db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.case_id == case_id,
            Document.company_id == scope.company_id,
        )
    ).scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    ensure_company_blob(document.blob_storage_path, scope.company_id)
    try:
        url = storage.get_download_url(document.blob_storage_path)
    except StorageOperationError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return DocumentFileUrlResponse(file_url=url)
