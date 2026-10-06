"""
`risk_settings` table — one row per company holding the two editable risk
tier cutoffs (SPECIFICATION.md section 3.3: "map to tier via configurable
thresholds"):

    score <  medium_threshold                      -> low
    medium_threshold <= score < high_threshold     -> medium
    score >= high_threshold                        -> high

Editing this row is safe for history: every `case_risk_assessments` row
embeds the thresholds it was scored with, so it never reads this table
after the fact.
"""
import uuid

from sqlalchemy import ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.company import TenantScopedMixin

DEFAULT_MEDIUM_THRESHOLD = 30
DEFAULT_HIGH_THRESHOLD = 60


class RiskSetting(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "risk_settings"
    # One row per company (thresholds are per company, like the rules).
    __table_args__ = (UniqueConstraint("company_id", name="uq_risk_settings_company_id"),)

    medium_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MEDIUM_THRESHOLD
    )
    high_threshold: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_HIGH_THRESHOLD
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
