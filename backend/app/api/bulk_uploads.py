"""
Bulk upload: many cases in one zip, with one top-level folder per case
(app/services/bulk_upload_service.py has the zip rules).

POST returns quickly (202). It streams the zip to a temporary file, never
holding more than the 300 MB limit, and rejects an over-limit body from its
Content-Length before reading it. It then checks the zip's central directory
(no decompression) and stores the zip. The bulk_uploads row it writes already
carries the per-case plan, including every structural failure, and it queues
ONE ingestion task. Unzipping, validating, creating the cases and queueing
their documents all happen in that task.

GET returns the summary screen's data. Each case's live status is derived on
every read from the case and its documents. The pipeline never reports
anything back to the upload.

Authorization is the same as the single-case upload: any company role may
bulk upload (platform admins may not, 403), and every case created from the
zip is submitted by the uploader. A `user` sees only their own bulk uploads.
Reviewers see all of their company's. Platform admins have read-only, audited
access (company_id query parameter, like the case list).
"""
from __future__ import annotations

import hashlib
import tempfile
import uuid
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.api.auth import get_tenant_db, require_company_role
from app.api.cases import _visible_flag
from app.api.tenant_access import CompanyScope, company_scope
from app.core.config import settings
from app.models.bulk_upload import BulkUpload, BulkUploadCase, BulkUploadStatus
from app.models.case import Case, CaseStatus, CaseType
from app.models.document import Document, DocumentProcessingStatus
from app.models.user import User, UserRole
from app.schemas.bulk_upload import (
    BulkUploadCaseSchema,
    BulkUploadDetail,
    BulkUploadFileSchema,
    BulkUploadProgress,
    BulkUploadSummary,
)
from app.schemas.case import CaseFlagSchema, UserSummary
from app.services.audit_service import record_event
from app.services.bulk_upload_service import ZIP_CONTENT_TYPE, inspect_zip, plan_rows, zip_too_large
from app.services.case_flag_service import get_case_flags
from app.services.storage_service import (
    StorageOperationError,
    StorageService,
    bulk_upload_blob_path,
    get_storage_service,
)
from app.services.upload_limits import company_upload_limits
from app.services.upload_validation import UploadRejected
from app.services.usage_service import record_bulk_zip_stored
from app.tasks.bulk_upload_task import ingest_bulk_upload

router = APIRouter(prefix="/bulk-uploads", tags=["bulk uploads"])

# Above this, the streamed zip moves from memory to a temporary file.
_SPOOL_IN_MEMORY_BYTES = 8 * 1024 * 1024
_DECIDED = (CaseStatus.approved, CaseStatus.rejected, CaseStatus.closed)
_TERMINAL_DOC = (DocumentProcessingStatus.complete, DocumentProcessingStatus.failed)


def _http(rejection: UploadRejected) -> HTTPException:
    return HTTPException(status_code=rejection.status_code, detail=rejection.detail())


@router.post(
    "",
    response_model=BulkUploadDetail,
    status_code=202,
    summary="Bulk upload: a zip of cases",
    description=(
        "Request body: the zip file itself (`Content-Type: application/zip`), not multipart. "
        "Query: `case_type` (applied to every case) and `filename` (the zip's name, for display). "
        "The zip holds one top-level folder per case, each containing that case's documents directly. "
        "The response (202) returns once the zip is stored: it lists every case folder found, with "
        "structural failures already marked (subfolder in a case folder, empty folder, empty or "
        "file over the company's per-file limit). Documents are then extracted, validated with the same checks as a single "
        "upload, and each valid case is created and queued, one case at a time. Poll GET "
        "/bulk-uploads/{id}. Partial success: a bad file is rejected on its own and a case with no valid "
        "file fails on its own. Zip rejections return `detail: {code, message, ...}` with `zip_empty`, "
        "`zip_too_large` (over the company's zip limit, `max_zip_size_mb`, 300 MB by default, decided from Content-Length before "
        "the body is read), `not_a_zip`, `zip_corrupted`, `zip_too_many_entries` or "
        "`zip_no_case_folders`."
    ),
    responses={
        400: {"description": "`zip_empty`."},
        401: {"description": "Missing, invalid or expired bearer token, or the user is inactive."},
        403: {"description": "Platform admins have read-only access."},
        413: {"description": "`zip_too_large` (`size_bytes`, `max_size_bytes`)."},
        415: {"description": "`not_a_zip`."},
        422: {"description": "`zip_corrupted`, `zip_too_many_entries`, `zip_no_case_folders`."},
        502: {"description": "Blob Storage rejected the upload."},
    },
)
async def create_bulk_upload(
    request: Request,
    case_type: CaseType = Query(..., description="Case type for every case in the zip."),
    filename: str = Query(..., min_length=1, max_length=512, description="The zip's file name."),
    current_user: User = Depends(require_company_role(UserRole.user)),
    db: Session = Depends(get_tenant_db),
    storage: StorageService = Depends(get_storage_service),
) -> BulkUploadDetail:
    company_id = current_user.company_id
    # This company's limits, read fresh on every request; then end the read
    # transaction before the (possibly long) body stream.
    limits = company_upload_limits(db, company_id)
    db.commit()
    max_bytes = limits.max_zip_bytes

    # Rejected before a single byte of the body is read.
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise _http(zip_too_large(int(declared), max_bytes))

    with tempfile.SpooledTemporaryFile(max_size=_SPOOL_IN_MEMORY_BYTES) as tmp:
        size = 0
        digest = hashlib.sha256()
        async for chunk in request.stream():
            size += len(chunk)
            if size > max_bytes:  # no/understated Content-Length: stop reading
                raise _http(zip_too_large(None, max_bytes))
            digest.update(chunk)
            tmp.write(chunk)

        try:
            inspection = await run_in_threadpool(
                inspect_zip, tmp, size,
                max_zip_bytes=max_bytes, max_file_bytes=limits.max_file_bytes,
            )
        except UploadRejected as rejection:
            raise _http(rejection) from None

        bulk_id = uuid.uuid4()
        file_hash = digest.hexdigest()
        tmp.seek(0)
        try:
            file_url = await run_in_threadpool(
                storage.upload, bulk_upload_blob_path(company_id, bulk_id, file_hash), tmp,
                ZIP_CONTENT_TYPE,
            )
        except StorageOperationError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    bulk = BulkUpload(
        id=bulk_id,
        company_id=company_id,
        uploaded_by_user_id=current_user.id,
        original_filename=filename,
        case_type=case_type,
        blob_storage_path=file_url,
        file_hash=file_hash,
        zip_size_bytes=size,
        status=BulkUploadStatus.queued,
        case_folder_count=inspection.case_folder_count,
        cases_failed=sum(1 for c in inspection.plan["cases"] if c["status"] == "failed"),
        documents_rejected=sum(
            1 for c in inspection.plan["cases"] for f in c["files"] if f["status"] == "rejected"
        ),
        zip_details={k: v for k, v in inspection.plan.items() if k != "cases"},
    )
    db.add(bulk)
    db.flush()
    db.add_all(plan_rows(bulk, inspection.plan))
    record_event(
        db,
        "bulk_upload_received",
        actor_user_id=current_user.id,
        event_data={
            "bulk_upload_id": str(bulk_id),
            "original_filename": filename,
            "file_hash": file_hash,
            "zip_size_bytes": size,
            "case_folder_count": inspection.case_folder_count,
        },
    )
    record_bulk_zip_stored(db, company_id, size)
    db.commit()

    # After the commit, so the worker can load the row.
    ingest_bulk_upload.delay(str(bulk_id), str(company_id))

    db.refresh(bulk)
    return _detail(db, current_user, bulk)


@router.get(
    "",
    response_model=list[BulkUploadSummary],
    summary="List bulk uploads",
    description="Newest first. A `user` sees only their own; reviewers see their company's. "
    "Platform admins pass `company_id` (audited).",
)
def list_bulk_uploads(
    limit: int = Query(default=20, ge=1, le=100),
    scope: CompanyScope = Depends(company_scope("bulk uploads")),
) -> list[BulkUploadSummary]:
    db, user = scope.db, scope.ctx.user
    stmt = (
        select(BulkUpload)
        .where(BulkUpload.company_id == scope.company_id)
        .order_by(BulkUpload.created_at.desc())
        .limit(limit)
    )
    if user.role == UserRole.user:
        stmt = stmt.where(BulkUpload.uploaded_by_user_id == user.id)
    rows = db.execute(stmt).scalars().all()
    users = _users(db, {b.uploaded_by_user_id for b in rows})
    return [_summary(b, users.get(b.uploaded_by_user_id)) for b in rows]


@router.get(
    "/{bulk_upload_id}",
    response_model=BulkUploadDetail,
    summary="Bulk upload summary (live per-case status)",
    description="Every case folder of the zip with its outcome (created / failed and why, per file) "
    "and, for created cases, live progress (queued → processing → done / flagged). Poll until "
    "`settled`. Another user's upload (for a `user`) or another company's is a 404.",
)
def get_bulk_upload(
    bulk_upload_id: uuid.UUID,
    scope: CompanyScope = Depends(company_scope("bulk upload")),
) -> BulkUploadDetail:
    db, user = scope.db, scope.ctx.user
    bulk = db.execute(
        select(BulkUpload).where(
            BulkUpload.id == bulk_upload_id, BulkUpload.company_id == scope.company_id
        )
    ).scalar_one_or_none()
    if bulk is None or (user.role == UserRole.user and bulk.uploaded_by_user_id != user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bulk upload not found")
    return _detail(db, user, bulk)


# ---------------------------------------------------------------------------


def _users(db: Session, ids: set[uuid.UUID]) -> dict[uuid.UUID, User]:
    if not ids:
        return {}
    return {u.id: u for u in db.execute(select(User).where(User.id.in_(ids))).scalars().all()}


def _summary(bulk: BulkUpload, uploader: User | None) -> BulkUploadSummary:
    return BulkUploadSummary(
        id=bulk.id,
        original_filename=bulk.original_filename,
        case_type=bulk.case_type,
        status=bulk.status,
        error_code=bulk.error_code,
        error_message=bulk.error_message,
        zip_size_bytes=bulk.zip_size_bytes,
        case_folder_count=bulk.case_folder_count,
        cases_created=bulk.cases_created,
        cases_failed=bulk.cases_failed,
        documents_accepted=bulk.documents_accepted,
        documents_rejected=bulk.documents_rejected,
        uploaded_by=UserSummary.model_validate(uploader) if uploader else None,
        created_at=bulk.created_at,
        started_at=bulk.started_at,
        finished_at=bulk.finished_at,
    )


def _detail(db: Session, user: User, bulk: BulkUpload) -> BulkUploadDetail:
    details = bulk.zip_details or {}
    company_id = bulk.company_id
    entries = db.execute(
        select(BulkUploadCase)
        .where(BulkUploadCase.bulk_upload_id == bulk.id, BulkUploadCase.company_id == company_id)
        .order_by(BulkUploadCase.folder_index)
    ).scalars().all()

    cases = db.execute(
        select(Case).where(Case.bulk_upload_id == bulk.id, Case.company_id == company_id)
    ).scalars().all()
    by_id = {c.id: c for c in cases}
    doc_counts: dict[uuid.UUID, dict[str, int]] = defaultdict(lambda: {"total": 0, "finished": 0, "started": 0})
    if by_id:
        for case_id, doc_status, n in db.execute(
            select(Document.case_id, Document.processing_status, func.count())
            .where(Document.case_id.in_(by_id), Document.company_id == company_id)
            .group_by(Document.case_id, Document.processing_status)
        ).all():
            counts = doc_counts[case_id]
            counts["total"] += n
            if doc_status in _TERMINAL_DOC:
                counts["finished"] += n
            if doc_status != DocumentProcessingStatus.pending:
                counts["started"] += n
    flags = get_case_flags(db, company_id, list(by_id))

    progress = BulkUploadProgress()
    rows: list[BulkUploadCaseSchema] = []
    for entry in entries:
        case = by_id.get(entry.case_id) if entry.case_id else None
        flag_schema: CaseFlagSchema | None = None
        counts = {"total": 0, "finished": 0, "started": 0}
        if entry.status == "failed":
            live = "failed"
        elif case is None:
            live = "validating"
        else:
            counts = doc_counts[case.id]
            case_flag = flags[case.id]
            flag_schema = _visible_flag(user, CaseFlagSchema.from_case_flag(case_flag))
            if case.status in _DECIDED:
                live = "done"
            elif case_flag.flag != "pending" and counts["finished"] == counts["total"]:
                # Risk signals stay hidden from submitters (see app/api/cases.py).
                flagged = case_flag.flag in ("medium", "high") and user.role != UserRole.user
                live = "flagged" if flagged else "done"
            elif counts["started"] == 0:
                live = "queued"
            else:
                live = "processing"
        setattr(progress, live, getattr(progress, live) + 1)
        rows.append(
            BulkUploadCaseSchema(
                index=entry.folder_index,
                folder=entry.folder,
                status=entry.status,
                error_code=entry.error_code,
                error_message=entry.error_message,
                case_id=entry.case_id,
                case_number=entry.case_number,
                files=[
                    BulkUploadFileSchema(
                        name=f["name"],
                        status=f["status"],
                        code=f.get("code"),
                        message=f.get("message"),
                        size_bytes=f.get("size_bytes"),
                        document_id=f.get("document_id"),
                    )
                    for f in entry.files
                ],
                live_status=live,
                case_status=case.status if case else None,
                documents_total=counts["total"],
                documents_finished=counts["finished"],
                flag=flag_schema,
            )
        )

    warnings: list[str] = []
    threshold = settings.bulk_upload_case_warning_threshold
    if (bulk.case_folder_count or 0) > threshold:
        warnings.append(
            f"This zip contains {bulk.case_folder_count} cases (more than {threshold}). They will all "
            "be processed, but results for the later cases will take longer to appear."
        )
    ingestion_over = bulk.status in (BulkUploadStatus.complete, BulkUploadStatus.failed)
    settled = ingestion_over and progress.validating == progress.queued == progress.processing == 0
    uploader = _users(db, {bulk.uploaded_by_user_id}).get(bulk.uploaded_by_user_id)
    return BulkUploadDetail(
        **_summary(bulk, uploader).model_dump(),
        wrapper_folder=details.get("wrapper_folder"),
        ignored_entries=details.get("ignored_entries", []),
        ignored_entry_count=details.get("ignored_entry_count", 0),
        warnings=warnings,
        progress=progress,
        settled=settled,
        cases=rows,
    )
