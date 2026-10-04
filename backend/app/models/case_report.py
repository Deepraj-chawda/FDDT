"""
`case_reports` table — one row per generated per-case PDF report (see
app/services/case_report_service.py).

Rows are only ever INSERTed. A report is a point-in-time record: it
reflects the case's evidence, risk assessment and risk-rule versions as
they were when it was generated, so two reports for the same case made at
different times may legitimately differ — history is kept, never
overwritten.

The PDF itself lives in Blob Storage under the `reports/` prefix, kept
separate from the original case documents (which live under
`<case_id>/...`). It is a NEW artifact built at export time; the original
document files are only ever read to produce it, never written.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UUIDPrimaryKeyMixin, utcnow
from app.models.company import TenantScopedMixin


class CaseReport(TenantScopedMixin, UUIDPrimaryKeyMixin, Base):
    __tablename__ = "case_reports"

    case_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cases.id"), nullable=False, index=True
    )
    generated_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    # Durable Blob Storage URL of the report PDF (the container is private;
    # readers get a short-lived signed URL via StorageService.get_download_url).
    blob_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    # SHA-256 of the report PDF's own bytes, so a copy of the report found
    # later can be tied back to the exact file that was generated.
    report_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # The risk assessment (and therefore the frozen risk-rule versions) this
    # report was built from. Null if the case had not been scored yet.
    risk_assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("case_risk_assessments.id"), nullable=True
    )

    case = relationship("Case")
    generated_by = relationship("User", foreign_keys=[generated_by_user_id])
    risk_assessment = relationship("CaseRiskAssessment")
