"""add pixel-identical signature reuse verdicts and their risk rules

Revision ID: e3a91d7c5b20
Revises: c8e2a5f19b04
Create Date: 2026-09-20 23:30:00.000000

Two new `signature_match_result` values from the classical pixel comparison
(app/services/signature_comparison_service.py `find_signature_reuse`):
`identical_reuse` and `reused_different_signer`, plus the two risk rules that
score them. Rules are inserted only if absent — a database created fresh from
migrations already got them from SEED_RULES in d4f8b2a61c37.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.services.risk_rule_seed import SEED_RULES

# revision identifiers, used by Alembic.
revision: str = "e3a91d7c5b20"
down_revision: Union[str, None] = "c8e2a5f19b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_VERDICTS = ("identical_reuse", "reused_different_signer")
NEW_RULE_IDS = ("signature.identical_reuse", "signature.reused_different_signer")


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't run inside a transaction block on older
    # Postgres versions, so step out of Alembic's transaction for it.
    with op.get_context().autocommit_block():
        for verdict in NEW_VERDICTS:
            op.execute(f"ALTER TYPE signature_match_result ADD VALUE IF NOT EXISTS '{verdict}'")

    bind = op.get_bind()
    existing = set(
        bind.execute(
            sa.text("SELECT rule_id FROM risk_rules WHERE rule_id = ANY(:ids)"),
            {"ids": list(NEW_RULE_IDS)},
        ).scalars()
    )
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
    rows = [
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
        if spec["rule_id"] in NEW_RULE_IDS and spec["rule_id"] not in existing
    ]
    if rows:
        op.bulk_insert(rules_table, rows)


def downgrade() -> None:
    # Deliberately non-destructive: Postgres can't drop an enum value, and the
    # comparison rows and scored history are evidence that must not be deleted.
    # Only switch the two rules off so they stop firing on new scoring runs.
    op.execute(
        "UPDATE risk_rules SET is_active = false "
        "WHERE rule_id IN ('signature.identical_reuse', 'signature.reused_different_signer')"
    )
