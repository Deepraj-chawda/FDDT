"""
`document_checks` table — one row per automated check run against a single
document (OCR/layout, classification, field extraction, each forensic
check, AI-generated-content detection, etc.). `result` holds the raw
check output as JSON; `confidence` backs the "fields_uncertain" routing
rule described in SPECIFICATION.md section 3.6.
"""
import enum
import uuid

from sqlalchemy import JSON, Enum, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.company import TenantScopedMixin


class DocumentCheckType(str, enum.Enum):
    ocr_layout = "ocr_layout"
    classification = "classification"
    field_extraction = "field_extraction"
    field_validation = "field_validation"
    issuer_verification = "issuer_verification"
    signature_stamp_detection = "signature_stamp_detection"
    metadata_forensics = "metadata_forensics"
    error_level_analysis = "error_level_analysis"
    copy_move_detection = "copy_move_detection"
    duplicate_detection = "duplicate_detection"
    ai_content_detection = "ai_content_detection"
    visual_inconsistency_review = "visual_inconsistency_review"
    font_consistency = "font_consistency"
    ghost_content = "ghost_content"


class DocumentCheckStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class DocumentCheck(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "document_checks"
    # One row per (document, check_type): a re-run (e.g. a Celery redelivery
    # under task_acks_late) overwrites it via app/services/check_store.py
    # instead of adding a duplicate. Every run is still in audit_log.
    __table_args__ = (
        UniqueConstraint("document_id", "check_type", name="uq_document_checks_document_check_type"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False, index=True
    )
    check_type: Mapped[DocumentCheckType] = mapped_column(
        Enum(DocumentCheckType, name="document_check_type"), nullable=False
    )
    status: Mapped[DocumentCheckStatus] = mapped_column(
        Enum(DocumentCheckStatus, name="document_check_status"),
        default=DocumentCheckStatus.pending,
        nullable=False,
    )
    result: Mapped[dict | None] = mapped_column(JSONB().with_variant(JSON(), "sqlite"), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    document = relationship("Document", back_populates="checks")
