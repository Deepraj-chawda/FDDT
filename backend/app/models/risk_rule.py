"""
`risk_rules` table — configuration for the transparent weighted rules
engine (SPECIFICATION.md section 3.3). Rules live in the DB, not hardcoded, so
an admin can tune weights/severity/wording/on-off without a redeploy.

VERSIONING (SPECIFICATION.md section 3.3 — firm requirement):
A row is IMMUTABLE once written. Editing a rule never UPDATEs it; the
admin API (app/api/settings.py) INSERTs a new row with the same
`rule_id` and `version + 1` instead. The "current" version of a rule is
simply its highest `version` — there is deliberately no mutable
"is_current" flag, so nothing here is ever rewritten after the fact.
`case_risk_assessments` snapshot the immutable row `id` + `version` of
every rule that fired, so a case scored under old weights keeps showing
exactly those weights and reasons forever, however the rule is tuned
later.

`is_active` is the admin's enable/disable switch (versioned like any
other edit) — not a "latest version" marker.

`condition` is the generic, engine-evaluable match definition (see
app/services/risk_scoring_service.py for the supported shapes). It is
seeded once and is not exposed for editing: what an admin tunes is the
weight, severity, on/off state, and (via a new version) the wording.
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow
from app.models.company import TenantScopedMixin


class RiskRule(TenantScopedMixin, UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "risk_rules"
    __table_args__ = (
        # Rules are per company: each company has its own independent copy of
        # every rule_id and its own version history.
        UniqueConstraint(
            "company_id", "rule_id", "version", name="uq_risk_rules_company_rule_id_version"
        ),
    )

    # Stable key shared by every version of the same rule, e.g.
    # "metadata.editing_software_detected".
    rule_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    # forensics | consistency | verification | duplication
    category: Mapped[str] = mapped_column(String(128), nullable=False)
    # Which check produces this signal (a document_checks.check_type value,
    # or "cross_document_consistency" / "signature_comparison" for the two
    # signals that don't come from a document_checks row).
    check_type: Mapped[str] = mapped_column(String(64), nullable=False)
    condition: Mapped[dict] = mapped_column(JSONB().with_variant(JSON(), "sqlite"), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_template: Mapped[str] = mapped_column(String(1024), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    # Who created THIS version (null for the initial seed).
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    # Optional admin-supplied reason for this version (shown in the trail).
    change_note: Mapped[str | None] = mapped_column(String(1024), nullable=True)
