"""calculation check risk rules

Revision ID: c4d8e2f6a9b1
Revises: f2c7a9e4b1d8
Create Date: 2026-10-06 21:00:00.000000

Field validation (app/services/field_validation_service.py) gained three
arithmetic sub-checks — line_item_arithmetic, tax_rate_consistency and
amount_in_words_consistency — scored by `field.line_item_arithmetic_mismatch`,
`field.tax_rate_mismatch` and `field.amount_in_words_mismatch`. Added to the
platform rule template and to every existing company (version 1), each only
if absent. No schema change: sub-check results live in document_checks.result,
and line items / the amount in words in documents.extracted_fields.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.services.risk_rule_seed import SEED_RULES

revision: str = "c4d8e2f6a9b1"
down_revision: Union[str, None] = "f2c7a9e4b1d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RULE_IDS = ("field.line_item_arithmetic_mismatch", "field.tax_rate_mismatch", "field.amount_in_words_mismatch")


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
                        "change_note": "Default rule set (calculation checks)",
                    }
                    for company_id in companies
                ],
            )


def downgrade() -> None:
    # Non-destructive, like e3a91d7c5b20: scored history references these
    # rules, so they are only switched off.
    ids = ", ".join(f"'{r}'" for r in RULE_IDS)
    op.execute(f"UPDATE risk_rules SET is_active = false WHERE rule_id IN ({ids})")
    op.execute(f"UPDATE risk_rule_templates SET is_active = false WHERE rule_id IN ({ids})")
