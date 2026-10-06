"""font consistency check and its risk rules

Revision ID: f2c7a9e4b1d8
Revises: e8b4c2d9f6a1
Create Date: 2026-10-06 18:00:00.000000

A new `document_check_type` value, `font_consistency`
(app/services/forensics/font_consistency.py): text set in a different font
family from the text around it, read from the PDF's text layer, or estimated
by OCR font recognition on scanned pages. Plus the two rules that score it,
`font.inconsistency` (text layer) and `font.inconsistency_scanned` (OCR), added
to the platform rule template and to every existing company (version 1), each
only if absent. Companies created later get them from the template like any
other rule.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.services.risk_rule_seed import SEED_RULES

revision: str = "f2c7a9e4b1d8"
down_revision: Union[str, None] = "e8b4c2d9f6a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RULE_IDS = ("font.inconsistency", "font.inconsistency_scanned")


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
    # ALTER TYPE ... ADD VALUE can't run inside a transaction block on older
    # Postgres versions, so step out of Alembic's transaction for it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE document_check_type ADD VALUE IF NOT EXISTS 'font_consistency'")

    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    templates = sa.table("risk_rule_templates", *_columns())
    rules = sa.table(
        "risk_rules",
        *_columns(),
        sa.column("company_id", postgresql.UUID(as_uuid=True)),
        sa.column("version", sa.Integer),
        sa.column("effective_from", sa.DateTime(timezone=True)),
        sa.column("change_note", sa.String),
    )
    for spec in (s for s in SEED_RULES if s["rule_id"] in RULE_IDS):
        common = {
            "rule_id": spec["rule_id"],
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
        if not bind.execute(
            sa.text("SELECT 1 FROM risk_rule_templates WHERE rule_id = :r"), {"r": spec["rule_id"]}
        ).first():
            op.bulk_insert(templates, [{"id": uuid.uuid4(), **common}])
        companies = bind.execute(
            sa.text(
                "SELECT id FROM companies c WHERE NOT EXISTS "
                "(SELECT 1 FROM risk_rules r WHERE r.company_id = c.id AND r.rule_id = :r)"
            ),
            {"r": spec["rule_id"]},
        ).scalars().all()
        if companies:
            op.bulk_insert(
                rules,
                [
                    {
                        "id": uuid.uuid4(),
                        **common,
                        "company_id": company_id,
                        "version": 1,
                        "effective_from": now,
                        "change_note": "Default rule set (font consistency check)",
                    }
                    for company_id in companies
                ],
            )


def downgrade() -> None:
    # Non-destructive, like e3a91d7c5b20: Postgres can't drop an enum value,
    # and scored history references these rules, so they are only switched
    # off and stop firing on new scoring runs.
    ids = ", ".join(f"'{r}'" for r in RULE_IDS)
    op.execute(f"UPDATE risk_rules SET is_active = false WHERE rule_id IN ({ids})")
    op.execute(f"UPDATE risk_rule_templates SET is_active = false WHERE rule_id IN ({ids})")
