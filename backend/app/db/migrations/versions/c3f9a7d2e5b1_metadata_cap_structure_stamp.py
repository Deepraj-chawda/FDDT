"""metadata score cap; editable-text-over-scan and stamp-vs-issuer rules

Revision ID: c3f9a7d2e5b1
Revises: b7e2d4f1c9a3
Create Date: 2026-10-07 21:00:00.000000

1. `risk_settings.metadata_score_cap` (default 40): the most points the
   metadata rules ("metadata.*") add together — one edit leaves several
   metadata traces (modified after created, the edit history, a date after
   the file's creation, the editor's name).
2. New rules, version 1, for every company that lacks them, and in the
   platform rule template: `metadata.editable_text_over_scan` (page
   structure, survives stripped XMP) and `signature.stamp_issuer_mismatch`.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.services.risk_rule_seed import SEED_RULES

revision: str = "c3f9a7d2e5b1"
down_revision: Union[str, None] = "b7e2d4f1c9a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_RULE_IDS = ("metadata.editable_text_over_scan", "signature.stamp_issuer_mismatch")


def _columns():  # fresh column objects per table (SQLAlchemy can't share them)
    return [
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
    ]


def upgrade() -> None:
    op.add_column(
        "risk_settings", sa.Column("metadata_score_cap", sa.Integer(), nullable=False, server_default="40")
    )

    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    specs = {s["rule_id"]: s for s in SEED_RULES}
    templates = sa.table("risk_rule_templates", *_columns())
    rules = sa.table(
        "risk_rules",
        *_columns(),
        sa.column("company_id", postgresql.UUID(as_uuid=True)),
        sa.column("version", sa.Integer),
        sa.column("effective_from", sa.DateTime(timezone=True)),
        sa.column("change_note", sa.String),
    )
    for rule_id in NEW_RULE_IDS:
        spec = specs[rule_id]
        common = {
            "rule_id": rule_id,
            "category": spec["category"],
            "check_type": spec["check_type"],
            "condition": spec["condition"],
            "weight": spec["weight"],
            "severity": spec["severity"],
            "reason_template": spec["reason_template"],
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        if not bind.execute(sa.text("SELECT 1 FROM risk_rule_templates WHERE rule_id = :r"), {"r": rule_id}).first():
            op.bulk_insert(templates, [{"id": uuid.uuid4(), **common}])
        companies = bind.execute(
            sa.text(
                "SELECT id FROM companies c WHERE NOT EXISTS "
                "(SELECT 1 FROM risk_rules r WHERE r.company_id = c.id AND r.rule_id = :r)"
            ),
            {"r": rule_id},
        ).scalars().all()
        if companies:
            op.bulk_insert(
                rules,
                [
                    {
                        "id": uuid.uuid4(), **common, "company_id": company_id, "version": 1,
                        "effective_from": now, "change_note": "Default rule set (structure and stamp checks)",
                    }
                    for company_id in companies
                ],
            )


def downgrade() -> None:
    # Rules are only switched off (scored history references them).
    ids = ", ".join(f"'{r}'" for r in NEW_RULE_IDS)
    op.execute(f"UPDATE risk_rules SET is_active = false WHERE rule_id IN ({ids})")
    op.execute(f"UPDATE risk_rule_templates SET is_active = false WHERE rule_id IN ({ids})")
    op.drop_column("risk_settings", "metadata_score_cap")
