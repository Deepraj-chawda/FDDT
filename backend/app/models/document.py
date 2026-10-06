"""
`documents` table — one row per uploaded file, grouped under a `case_id`.

Original files are stored immutably in Azure Blob Storage; `file_hash` is
the SHA-256 of the original bytes, recorded at upload time so the stored
original can be integrity-checked later (SPECIFICATION.md section 3.5, Evidence
repository). `document_type` is filled in later by the classification
step — it is nullable at upload time.

`processing_status` / `extracted_fields` / `ocr_text` / `processing_error`
are filled in by the OCR + classification + extraction Celery pipeline
(app/tasks/document_processing.py). Forensics, issuer verification,
cross-document checks, and risk scoring are separate, later steps — none
of that lives on this model.
"""
import enum
import uuid

from sqlalchemy import Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSON, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.company import TenantScopedMixin


class DocumentProcessingStatus(str, enum.Enum):
    pending = "pending"  # uploaded, Celery task not picked up yet
    processing = "processing"
    complete = "complete"
    failed = "failed"


class Document(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "documents"

    case_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cases.id"), nullable=False, index=True
    )
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    blob_storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Filled in later by the classification service; no fixed enum here on
    # purpose — the label list is configurable, not hardcoded (see
    # app/services/classification_service.py's DOCUMENT_TYPE_LABELS).
    document_type: Mapped[str | None] = mapped_column(String(128), nullable=True)

    processing_status: Mapped[DocumentProcessingStatus] = mapped_column(
        Enum(DocumentProcessingStatus, name="document_processing_status"),
        default=DocumentProcessingStatus.pending,
        nullable=False,
    )
    # Structured extraction output: {"core_fields": {...}, "additional_fields": [...]}
    # — each field carries its own {value, confidence, uncertain}, per
    # SPECIFICATION.md section 3.6. JSON on SQLite so the test suite (in-memory
    # SQLite, see tests/conftest.py) doesn't need Postgres-only JSONB.
    extracted_fields: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    # Full OCR/layout text (Azure Document Intelligence's `.content`) —
    # kept for evidence/audit purposes and so extraction quality (e.g. on
    # Arabic documents) can be checked against what the model actually saw.
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    case = relationship("Case", back_populates="documents")
    checks = relationship("DocumentCheck", back_populates="document")
