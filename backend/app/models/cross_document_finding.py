"""
`cross_document_findings` table — case-level reconciliation results
(SPECIFICATION.md section 3.1, "Cross-document validation & reconciliation"):
shared fields (amount, date, vendor, ...) compared pairwise across every
document in a case, once all of that case's documents have been processed.
"""
import enum
import uuid

from sqlalchemy import JSON, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.company import TenantScopedMixin


class FindingSeverity(str, enum.Enum):
    info = "info"
    low = "low"
    medium = "medium"
    high = "high"


class CrossDocumentFinding(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "cross_document_findings"

    case_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cases.id"), nullable=False, index=True
    )
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    finding_type: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[FindingSeverity] = mapped_column(
        Enum(FindingSeverity, name="finding_severity"), default=FindingSeverity.low, nullable=False
    )
    description: Mapped[str] = mapped_column(String(2048), nullable=False)
    # IDs (as strings) of the documents involved in this finding.
    document_ids: Mapped[list | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )

    case = relationship("Case", back_populates="cross_document_findings")
