"""
`risk_scores` table — one row per scoring run of a case. `triggered_reasons`
holds the filled-in `reason_template` of every rule that fired (this is
what reviewers actually read, not just the numeric score).

`rules_snapshot` freezes the {rule_id: weight} pairs that were active at
scoring time, so a later change to `risk_rules.weight` never retroactively
changes a historical case's score (SPECIFICATION.md section 3.3, rule versioning).
"""
import uuid

from sqlalchemy import JSON, Enum, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.company import TenantScopedMixin
from app.models.case import RiskTier


class RiskScore(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "risk_scores"

    case_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("cases.id"), nullable=False, index=True
    )
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    tier: Mapped[RiskTier] = mapped_column(Enum(RiskTier, name="risk_score_tier"), nullable=False)
    triggered_reasons: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list
    )
    # Snapshot of {rule_id: weight} active at scoring time — see module
    # docstring. Never mutated after the fact.
    rules_snapshot: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict
    )

    case = relationship("Case", back_populates="risk_scores")
