import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.models.bulk_upload import BulkUploadStatus
from app.models.case import CaseStatus, CaseType
from app.schemas.case import CaseFlagSchema, UserSummary

# Per-case progress on the bulk-upload summary screen, derived on every read
# from the case and its documents (never stored, never reported back by the
# pipeline):
#   validating  the ingestion task hasn't reached this case folder yet
#   failed      the folder was rejected (no case created)
#   queued      case created, none of its documents picked up by a worker yet
#   processing  some automated checks still running
#   done        every check finished (or a reviewer already decided the case)
#   flagged     done, with a medium/high risk tier (reviewers only; a
#               submitter sees "done" — risk signals are never shown to them)
LiveStatus = Literal["validating", "failed", "queued", "processing", "done", "flagged"]


class BulkUploadFileSchema(BaseModel):
    name: str
    # skipped: the whole folder was rejected (subfolder), so the file was never examined.
    status: Literal["pending", "accepted", "rejected", "skipped"]
    code: str | None = None
    message: str | None = None
    size_bytes: int | None = None
    document_id: uuid.UUID | None = None


class BulkUploadCaseSchema(BaseModel):
    index: int
    folder: str
    status: Literal["pending", "created", "failed"]
    error_code: str | None = None
    error_message: str | None = None
    case_id: uuid.UUID | None = None
    case_number: str | None = None
    files: list[BulkUploadFileSchema]
    live_status: LiveStatus
    case_status: CaseStatus | None = None
    documents_total: int = 0
    documents_finished: int = 0
    flag: CaseFlagSchema | None = None


class BulkUploadProgress(BaseModel):
    validating: int = 0
    failed: int = 0
    queued: int = 0
    processing: int = 0
    done: int = 0
    flagged: int = 0


class BulkUploadSummary(BaseModel):
    id: uuid.UUID
    original_filename: str
    case_type: CaseType
    status: BulkUploadStatus
    error_code: str | None
    error_message: str | None
    zip_size_bytes: int
    case_folder_count: int | None
    cases_created: int
    cases_failed: int
    documents_accepted: int
    documents_rejected: int
    uploaded_by: UserSummary | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class BulkUploadDetail(BulkUploadSummary):
    wrapper_folder: str | None = None
    ignored_entries: list[str] = []
    ignored_entry_count: int = 0
    warnings: list[str] = []
    progress: BulkUploadProgress
    # True once nothing on the screen can change any more: ingestion is over
    # and every created case is done (or decided). The frontend stops
    # polling then.
    settled: bool
    cases: list[BulkUploadCaseSchema]
