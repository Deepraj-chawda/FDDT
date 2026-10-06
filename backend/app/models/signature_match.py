"""
`signature_matches` table — one row per (signature_reference,
target_document) comparison. Populated by the Celery task
`run_signature_comparison` (app/tasks/signature_comparison_task.py)
immediately after a reviewer creates a reference, comparing against
every other document in the same upload batch/case.

`comparison_scope` is an enum kept extensible for future library-wide
comparison ("library") even though only "in_case" is generated today
(SPECIFICATION.md §2.3: "keep 'library' as a valid value for later").

`result` uses advisory language deliberately — SPECIFICATION.md §2.3 and §4
forbid UI copy or code that reads as "signature verified". The first four
values map to the four verdicts the vision model is asked for. The last two
come from a classical pixel comparison instead (no model — see
`find_signature_reuse` in app/services/signature_comparison_service.py):
a handwritten signature never repeats pixel-for-pixel, so an exact match
means the same image file was inserted twice.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utcnow
from app.models.company import TenantScopedMixin


class ComparisonScope(str, enum.Enum):
    in_case = "in_case"
    library = "library"  # Reserved for a future phase — no logic built yet.


class SignatureMatchResult(str, enum.Enum):
    consistent = "consistent"
    possibly_consistent = "possibly_consistent"
    inconsistent = "inconsistent"
    cannot_determine = "cannot_determine"
    # Pixel-identical to the reference signature (a reused image, not a fresh signing).
    identical_reuse = "identical_reuse"
    # Same, and the printed signer text under the two signatures differs.
    reused_different_signer = "reused_different_signer"


class SignatureMatch(TenantScopedMixin, Base):
    __tablename__ = "signature_matches"
    # One result per (reference, target document, scope) — a retried or
    # concurrent comparison task can't store the same pair twice.
    __table_args__ = (
        UniqueConstraint(
            "signature_reference_id", "document_id", "comparison_scope", name="uq_signature_matches_ref_doc_scope"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False, index=True
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cases.id"), nullable=False, index=True
    )
    signature_reference_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("signature_references.id"),
        nullable=False,
        index=True,
    )

    comparison_scope: Mapped[ComparisonScope] = mapped_column(
        Enum(ComparisonScope, name="comparison_scope"), nullable=False
    )
    result: Mapped[SignatureMatchResult] = mapped_column(
        Enum(SignatureMatchResult, name="signature_match_result"), nullable=False
    )
    # Vision model's qualitative explanation in plain English — advisory,
    # not a confidence score (SPECIFICATION.md §2.3: "qualitative judgment only —
    # never a numeric score implying precision this technique doesn't have").
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    compared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    document = relationship("Document", foreign_keys=[document_id])
    case = relationship("Case", foreign_keys=[case_id])
    reference = relationship("SignatureReference", back_populates="matches")
