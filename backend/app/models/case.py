"""
`cases` table — the unit of work the state machine (`transitions` library,
see app/services/workflow_service.py, to be implemented later) operates on.

`status` mirrors the state machine in SPECIFICATION.md section 3.4:
submitted -> under_automated_review -> pending_manual_review / auto_approved
  -> escalated -> under_investigation -> approved/rejected -> closed

`assigned_tier` records which reviewer tier owns the case (see CaseTier).

`external_ref_id` is a nullable placeholder column (see SPECIFICATION.md section 4
note) for a possible future external case-management integration — it is
not used by anything yet.
"""
import enum
import uuid

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.company import TenantScopedMixin


class CaseStatus(str, enum.Enum):
    submitted = "submitted"
    under_automated_review = "under_automated_review"
    pending_manual_review = "pending_manual_review"
    auto_approved = "auto_approved"
    escalated = "escalated"
    under_investigation = "under_investigation"
    approved = "approved"
    rejected = "rejected"
    closed = "closed"


class RiskTier(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"


class CaseTier(str, enum.Enum):
    """Which reviewer tier owns the case — orthogonal to `status`. Every case
    starts at `l1`; escalating it moves it to `l2` (one-directional, there is
    no de-escalation). On an `l2` case a `reviewer_l1` keeps read access but
    every action endpoint returns 403; `reviewer_l2` and `admin` can act on
    cases of either tier."""

    l1 = "l1"
    l2 = "l2"


class CaseType(str, enum.Enum):
    """Case-level category, selected at submission time (System Specification section
    3.6's document-type coverage list). This is separate from a document's
    own `document_type`, which is filled in later by the classification
    step — a case can hold documents of more than one type (e.g. an
    invoice plus its approval voucher)."""

    school_document = "school_document"
    vendor_invoice = "vendor_invoice"
    commercial_invoice = "commercial_invoice"
    procurement_documentation = "procurement_documentation"
    quotation = "quotation"
    travel_reimbursement = "travel_reimbursement"
    other = "other"


class Case(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "cases"

    case_number: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    case_type: Mapped[CaseType] = mapped_column(Enum(CaseType, name="case_type"), nullable=False)
    submitted_by_user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    status: Mapped[CaseStatus] = mapped_column(
        Enum(CaseStatus, name="case_status"), default=CaseStatus.submitted, nullable=False
    )
    # Denormalized copy of the latest case_risk_assessments.tier, kept in
    # sync by app/services/risk_scoring_service.py so the queue can sort/
    # filter on it without a join. The assessment row is the source of truth.
    risk_tier: Mapped[RiskTier | None] = mapped_column(
        Enum(RiskTier, name="risk_tier"), nullable=True
    )
    assigned_tier: Mapped[CaseTier] = mapped_column(
        Enum(CaseTier, name="case_tier"),
        default=CaseTier.l1,
        server_default=CaseTier.l1.value,
        nullable=False,
    )
    # Placeholder for a possible future external case-management integration
    # (see SPECIFICATION.md section 4) — unused for now.
    external_ref_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Set on cases created from a bulk zip upload (app/models/bulk_upload.py):
    # which upload, and the uploader's own label for the case, which is its
    # folder name inside the zip. The label is only a reference for the
    # uploader. It need not be unique, and the case's identity is still its
    # id / case_number.
    bulk_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("bulk_uploads.id"), nullable=True, index=True
    )
    reference_label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    submitted_by = relationship(
        "User", back_populates="submitted_cases", foreign_keys=[submitted_by_user_id]
    )
    documents = relationship("Document", back_populates="case")
    cross_document_findings = relationship("CrossDocumentFinding", back_populates="case")
    # Legacy stub table from the initial schema; superseded by
    # `risk_assessments` (case_risk_assessments) and no longer written.
    risk_scores = relationship("RiskScore", back_populates="case")
    risk_assessments = relationship(
        "CaseRiskAssessment", back_populates="case", order_by="CaseRiskAssessment.computed_at"
    )
    actions = relationship("CaseAction", back_populates="case")
