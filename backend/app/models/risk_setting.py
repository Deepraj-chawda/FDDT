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
# Most points the metadata rules (rule_id "metadata.*") may add together. One
# edit leaves several metadata traces (modified after created, the edit
# history, a date after the file's creation, the editor's name); without a
# cap they alone can max the score. 100 = no cap.
DEFAULT_METADATA_SCORE_CAP = 40


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
    metadata_score_cap: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_METADATA_SCORE_CAP, server_default=str(DEFAULT_METADATA_SCORE_CAP)
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
