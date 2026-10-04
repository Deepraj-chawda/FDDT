"""add_risk_engine_and_workflow

Part A/B/C of the risk-scoring + settings + workflow work:

- risk_rules: becomes VERSIONED. Adds check_type, version, effective_from,
  updated_by, change_note; swaps the global UNIQUE(rule_id) for
  UNIQUE(rule_id, version) so an edit can INSERT a new version instead of
  updating a row historical assessments reference. Seeds the initial rules.
- risk_settings: single row of admin-editable low/medium/high cutoffs.
- case_risk_assessments: one row per scoring run (append-only).
- cases.priority (normal|escalated): escalation is a priority flag, not a
  status.
- issuer_registry.is_active: soft delete for the admin registry CRUD (the
  only schema addition to that table).

Revision ID: d4f8b2a61c37
Revises: 15437688d14c
Create Date: 2026-09-19 23:10:00.000000

"""
import json
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.services.risk_rule_seed import SEED_RULES

# revision identifiers, used by Alembic.
revision: str = "d4f8b2a61c37"
down_revision: Union[str, None] = "15437688d14c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ---- risk_rules: versioning -------------------------------------------
    op.add_column("risk_rules", sa.Column("check_type", sa.String(64), nullable=False, server_default=""))
    op.add_column("risk_rules", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column(
        "risk_rules",
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.add_column("risk_rules", sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("risk_rules", sa.Column("change_note", sa.String(1024), nullable=True))
    op.create_foreign_key("fk_risk_rules_updated_by_users", "risk_rules", "users", ["updated_by"], ["id"])
    op.alter_column("risk_rules", "check_type", server_default=None)
    op.alter_column("risk_rules", "version", server_default=None)
    op.alter_column("risk_rules", "effective_from", server_default=None)

    op.drop_constraint("risk_rules_rule_id_key", "risk_rules", type_="unique")
    op.create_unique_constraint("uq_risk_rules_rule_id_version", "risk_rules", ["rule_id", "version"])

    # ---- risk_settings ----------------------------------------------------
    op.create_table(
        "risk_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("medium_threshold", sa.Integer(), nullable=False),
        sa.Column("high_threshold", sa.Integer(), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
    )

    # ---- case_risk_assessments -------------------------------------------
    op.create_table(
        "case_risk_assessments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cases.id"), nullable=False),
        sa.Column("raw_score", sa.Float(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        # `risk_tier` already exists (created for cases.risk_tier) — reuse it.
        sa.Column(
            "tier",
            postgresql.ENUM("low", "medium", "high", name="risk_tier", create_type=False),
            nullable=False,
        ),
        sa.Column("triggered_reasons", postgresql.JSONB(), nullable=False),
        sa.Column("risk_rules_version_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(64), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_case_risk_assessments_case_id", "case_risk_assessments", ["case_id"])
    op.create_index("ix_case_risk_assessments_computed_at", "case_risk_assessments", ["computed_at"])
    op.create_index(
        "ix_case_risk_assessments_evidence_fingerprint", "case_risk_assessments", ["evidence_fingerprint"]
    )

    # ---- cases.priority ---------------------------------------------------
    case_priority = postgresql.ENUM("normal", "escalated", name="case_priority")
    case_priority.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "cases",
        sa.Column(
            "priority",
            postgresql.ENUM("normal", "escalated", name="case_priority", create_type=False),
            nullable=False,
            server_default="normal",
        ),
    )

    # ---- issuer_registry.is_active ---------------------------------------
    op.add_column(
        "issuer_registry",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    # ---- seed -------------------------------------------------------------
    now = datetime.now(timezone.utc)
    rules_table = sa.table(
        "risk_rules",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("rule_id", sa.String),
        sa.column("category", sa.String),
        sa.column("check_type", sa.String),
        sa.column("condition", postgresql.JSONB),
        sa.column("weight", sa.Float),
        sa.column("severity", sa.String),
        sa.column("reason_template", sa.String),
        sa.column("is_active", sa.Boolean),
        sa.column("version", sa.Integer),
        sa.column("effective_from", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        rules_table,
        [
            {
                "id": uuid.uuid4(),
                "created_at": now,
                "updated_at": now,
                "rule_id": spec["rule_id"],
                "category": spec["category"],
                "check_type": spec["check_type"],
                "condition": spec["condition"],
                "weight": spec["weight"],
                "severity": spec["severity"],
                "reason_template": spec["reason_template"],
                "is_active": True,
                "version": 1,
                "effective_from": now,
            }
            for spec in SEED_RULES
        ],
    )
    settings_table = sa.table(
        "risk_settings",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("medium_threshold", sa.Integer),
        sa.column("high_threshold", sa.Integer),
    )
    op.bulk_insert(
        settings_table,
        [{"id": uuid.uuid4(), "created_at": now, "updated_at": now, "medium_threshold": 30, "high_threshold": 60}],
    )


def downgrade() -> None:
    op.drop_column("issuer_registry", "is_active")
    op.drop_column("cases", "priority")
    op.execute("DROP TYPE IF EXISTS case_priority")
    op.drop_index("ix_case_risk_assessments_evidence_fingerprint", table_name="case_risk_assessments")
    op.drop_index("ix_case_risk_assessments_computed_at", table_name="case_risk_assessments")
    op.drop_index("ix_case_risk_assessments_case_id", table_name="case_risk_assessments")
    op.drop_table("case_risk_assessments")
    op.drop_table("risk_settings")
    # Before this migration risk_rules was empty (a stub table), so a faithful
    # downgrade removes every rule row — the seeded ones AND any admin-created
    # versions. (Also lets `upgrade` re-seed cleanly after a downgrade, and
    # sidesteps the old global UNIQUE(rule_id) that versioned rows can't satisfy.)
    op.execute("DELETE FROM risk_rules")
    op.drop_constraint("uq_risk_rules_rule_id_version", "risk_rules", type_="unique")
    op.create_unique_constraint("risk_rules_rule_id_key", "risk_rules", ["rule_id"])
    op.drop_constraint("fk_risk_rules_updated_by_users", "risk_rules", type_="foreignkey")
    for col in ("change_note", "updated_by", "effective_from", "version", "check_type"):
        op.drop_column("risk_rules", col)
