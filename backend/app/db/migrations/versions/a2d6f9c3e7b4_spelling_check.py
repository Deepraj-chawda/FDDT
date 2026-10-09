"""spelling check (English and Arabic)

Revision ID: a2d6f9c3e7b4
Revises: f3b7d1e8a4c5
Create Date: 2026-10-09 12:00:00.000000

  - a new `document_check_type` value, `spelling_consistency`
    (app/services/forensics/spelling.py);
  - a new rule, version 1, for every company that lacks it, and in the
    platform rule template: `content.spelling_error` (weight 5, low).
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.services.risk_rule_seed import SEED_RULES

revision: str = "a2d6f9c3e7b4"
down_revision: Union[str, None] = "f3b7d1e8a4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_RULE_IDS = ("content.spelling_error",)


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


def _rules_table():
    return sa.table(
        "risk_rules",
        *_columns(),
        sa.column("company_id", postgresql.UUID(as_uuid=True)),
        sa.column("version", sa.Integer),
        sa.column("effective_from", sa.DateTime(timezone=True)),
        sa.column("change_note", sa.String),
    )


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't run inside a transaction block on older
    # Postgres versions, so step out of Alembic's transaction for it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE document_check_type ADD VALUE IF NOT EXISTS 'spelling_consistency'")

    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    specs = {s["rule_id"]: s for s in SEED_RULES}
    templates = sa.table("risk_rule_templates", *_columns())
    rules = _rules_table()

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
                        "effective_from": now, "change_note": "Default rule set (spelling check)",
                    }
                    for company_id in companies
                ],
            )


def downgrade() -> None:
    # Non-destructive, like the earlier rule migrations: scored history
    # references every version row, so the new rule is only switched off
    # (and a Postgres enum value cannot be dropped).
    ids = ", ".join(f"'{r}'" for r in NEW_RULE_IDS)
    op.execute(f"UPDATE risk_rules SET is_active = false WHERE rule_id IN ({ids})")
    op.execute(f"UPDATE risk_rule_templates SET is_active = false WHERE rule_id IN ({ids})")
