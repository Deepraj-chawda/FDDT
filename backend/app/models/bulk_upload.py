"""
`bulk_uploads` table — one row per zip of cases (app/api/bulk_uploads.py).

The zip is only a way to get cases in. Its ingestion task (app/tasks/
bulk_upload_task.py) unzips it, validates every document, creates the cases
and documents and queues each document's ordinary per-document pipeline, then
its work is done. Pipeline tasks never read or write this row and never wait
on it. There is no batch for them to report back to. The bulk-upload summary
screen reads each case's live status from the cases themselves
(`cases.bulk_upload_id`).

Per-case outcomes live in `bulk_upload_cases`, one row per case folder
written ONLY by the upload request (the plan) and the ingestion task. Each
case's update is one small row, so a zip of thousands of cases never rewrites
a growing blob. `zip_details` holds the zip-level notes:

    {"wrapper_folder": "claims-oct" | null,
     "ignored_entries": ["readme.txt", ...],   # loose files at the root
     "ignored_entry_count": 1}

The row is kept after ingestion together with the zip in Blob Storage. That
keeps a record of exactly what was submitted. The documents extracted from the
zip are the evidence originals, stored like any single upload.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.case import CaseType
from app.models.company import TenantScopedMixin


class BulkUploadStatus(str, enum.Enum):
    queued = "queued"  # zip stored, ingestion task not started yet
    ingesting = "ingesting"  # unzipping / validating / creating cases
    complete = "complete"  # every case folder handled (some may have failed)
    failed = "failed"  # the zip itself could not be ingested (see error_code)


class BulkUpload(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bulk_uploads"

    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    # The case type every case created from this zip gets (chosen at upload,
    # like the single-case form's case type).
    case_type: Mapped[CaseType] = mapped_column(Enum(CaseType, name="case_type"), nullable=False)
    blob_storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    zip_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    status: Mapped[BulkUploadStatus] = mapped_column(
        Enum(BulkUploadStatus, name="bulk_upload_status"),
        default=BulkUploadStatus.queued,
        nullable=False,
    )
    # Zip-level failure (status=failed): a stable code + user-facing message.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    case_folder_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cases_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cases_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    documents_accepted: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    documents_rejected: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    zip_details: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BulkUploadCase(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One case folder of a bulk upload and what became of it.

    `status`: pending (not ingested yet) → created (case_id set) | failed
    (error_code/error_message say why: case_nested_folder, case_no_documents,
    case_no_valid_documents). `files` is that folder's per-file outcome:

        [{"name": "invoice.pdf", "entry_index": 3, "size_bytes": 1234,
          "status": "pending" | "accepted" | "rejected" | "skipped",
          "code": null | "file_corrupted" | ..., "message": null | "...",
          "document_id": null | "<uuid>"}]
    """

    __tablename__ = "bulk_upload_cases"
    __table_args__ = (UniqueConstraint("bulk_upload_id", "folder_index"),)

    bulk_upload_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bulk_uploads.id"), nullable=False, index=True
    )
    folder_index: Mapped[int] = mapped_column(Integer, nullable=False)
    folder: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cases.id"), nullable=True
    )
    case_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    files: Mapped[list] = mapped_column(JSONB().with_variant(JSON(), "sqlite"), nullable=False)
