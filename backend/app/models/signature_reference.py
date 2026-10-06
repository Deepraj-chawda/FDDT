"""
`signature_references` table — one row per reviewer-created signature
region that can be used for in-case (and eventually library-wide)
comparison.

`person_name` is REQUIRED and manually typed by the reviewer who
creates the reference — the system never attempts to auto-extract or
pre-fill it (SPECIFICATION.md §2.3: "manual signer name entry").

`is_library=True` rows are saved for future cross-case use (data
collection only at this phase — no automatic library-wide comparison
logic runs against them yet). `is_library=False` rows are scoped to
`source_case_id` only.
"""
import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSON, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.company import TenantScopedMixin


class SignatureReference(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "signature_references"

    # Reviewer-typed person name — no auto-extraction, no pre-fill.
    person_name: Mapped[str] = mapped_column(Text, nullable=False)

    # Durable Blob Storage URL for the cropped signature image PNG.
    signature_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Normalized bounding box: {page (1-based), x, y, width, height} —
    # same 0-1 page-fraction convention as the rest of the overlay
    # system (app/services/forensics/ela.py, PdfOverlayViewer.tsx).
    bounding_box: Mapped[dict | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )

    source_document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False, index=True
    )
    source_case_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cases.id"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )

    # False = in-case reference only (used for same-case comparison).
    # True  = saved to the permanent signature library for future use.
    # NOTE: No automatic cross-case comparison logic runs against library
    # rows at this phase — is_library=True is data collection only.
    is_library: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    source_document = relationship("Document", foreign_keys=[source_document_id])
    source_case = relationship("Case", foreign_keys=[source_case_id])
    created_by_user = relationship("User", foreign_keys=[created_by])
    matches = relationship("SignatureMatch", back_populates="reference")
