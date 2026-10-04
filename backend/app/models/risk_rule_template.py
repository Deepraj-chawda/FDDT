"""
`risk_rule_templates` table — the platform-level default rule set (no
company_id). Platform admins edit it (Platform › Rule templates).

When a company is created, every ACTIVE template row is copied into that
company's own `risk_rules` (version 1, company_id stamped) — see
`seed_risk_rules` in app/services/risk_rule_seed.py. From then on the
company's copy is independent: editing a template never changes an existing
company's rules (the same "no retroactive change" principle as rule
versioning), it only affects companies created afterwards.

Templates are plain mutable rows (no version history of their own); every
create/edit writes a platform-level audit_log event with the old and new
values.
"""
import uuid

from sqlalchemy import JSON, Boolean, Float, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RiskRuleTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "risk_rule_templates"

    rule_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    check_type: Mapped[str] = mapped_column(String(64), nullable=False)
    condition: Mapped[dict] = mapped_column(JSONB().with_variant(JSON(), "sqlite"), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_template: Mapped[str] = mapped_column(String(1024), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
