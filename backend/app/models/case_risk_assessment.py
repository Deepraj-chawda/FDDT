"""
`case_risk_assessments` table — one row per scoring run of a case
(SPECIFICATION.md section 3.3). Rows are only ever INSERTed, never updated or
overwritten, so a case's scoring history is preserved in full.

`triggered_reasons` stores the already-rendered reason text of every rule
that fired (plus severity/weight/document), so what a reviewer read at
scoring time is frozen as text — later edits to a rule's wording or
weight cannot change it.

`risk_rules_version_snapshot` records exactly which immutable rule
VERSIONS produced the score ({"rules": [{rule_pk, rule_id, version,
weight, severity}, ...], "thresholds": {medium, high}}) — never "current
rules by rule_id alone".
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UUIDPrimaryKeyMixin, utcnow
from app.models.company import TenantScopedMixin
from app.models.case import RiskTier


class CaseRiskAssessment(TenantScopedMixin, UUIDPrimaryKeyMixin, Base):
    __tablename__ = "case_risk_assessments"
    # The history is append-only, but scoring the same evidence twice (two
    # concurrent score_case tasks, or a redelivered one) must not add a second
    # identical assessment.
    __table_args__ = (
        UniqueConstraint("case_id", "evidence_fingerprint", name="uq_case_risk_assessments_case_evidence"),
    )

    case_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cases.id"), nullable=False, index=True
    )
    # Straight sum of fired weights, before capping/flooring.
    raw_score: Mapped[float] = mapped_column(Float, nullable=False)
    # raw_score clamped to 0-100 — the value the tier is derived from.
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    tier: Mapped[RiskTier] = mapped_column(Enum(RiskTier, name="risk_tier"), nullable=False)
    triggered_reasons: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list
    )
    risk_rules_version_snapshot: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict
    )
    # Hash of the EVIDENCE that was scored (documents + which rules fired
    # where) — not of rule weights. Lets a re-run over unchanged evidence
    # be skipped, so a stray re-trigger never re-scores an old case under
    # newly tuned weights.
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )

    case = relationship("Case", back_populates="risk_assessments")
