"""ghost-content check; duplicate font subsets; widened scan structure; scrubbed producer

Revision ID: d5e8b3a1f7c2
Revises: c3f9a7d2e5b1
Create Date: 2026-10-07 23:30:00.000000

Edited-scan checks from CASE-39CB18BF (a scan converted to editable text in
Acrobat, bank details deleted, amounts retyped, metadata scrubbed). Same
versioning as b7e2d4f1c9a3 — never an UPDATE of a rule row:
  - a new `document_check_type` value, `ghost_content`
    (app/services/forensics/ghost_content.py);
  - NEW VERSIONS for every company: `font.inconsistency` v3 (text in a
    second embedded copy of a font counts) and
    `metadata.editable_text_over_scan` v2 (whatever the fonts are called).
    The company's own weight, severity and on/off state are carried over;
    the condition and (unless the company reworded it) the reason text come
    from the new definition;
  - new rules, version 1, for every company that lacks them, and in the
    platform rule template: `metadata.producer_scrubbed`,
    `content.deleted_ghost_block`, `content.replaced_ghost_line`.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.services.risk_rule_seed import SEED_RULES

revision: str = "d5e8b3a1f7c2"
down_revision: Union[str, None] = "c3f9a7d2e5b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_RULE_IDS = ("metadata.producer_scrubbed", "content.deleted_ghost_block", "content.replaced_ghost_line")
CHANGE_NOTES = {
    "font.inconsistency": (
        "v3: text in a second embedded copy (subset) of a font the page already embeds, beside text in the "
        "main copy, counts too"
    ),
    "metadata.editable_text_over_scan": (
        "v2: a scan with visible editable text over it counts whatever the fonts are called, not only a "
        "converter's \"-NNNN\" fonts"
    ),
}
# The reason text each changed rule had before — a company still using it
# gets the new wording; one that reworded it keeps its own.
OLD_REASONS = {
    "font.inconsistency": (
        "'{document}': {count} piece(s) of text are set in a different font, or at a different size, from the "
        "text around them — a sign they were typed in after the document was produced. {description}"
    ),
    "metadata.editable_text_over_scan": (
        "'{document}' is a scan converted to editable text — its numbers can be retyped like a word-processor "
        "file. {description}"
    ),
}


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
        op.execute("ALTER TYPE document_check_type ADD VALUE IF NOT EXISTS 'ghost_content'")

    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    specs = {s["rule_id"]: s for s in SEED_RULES}
    templates = sa.table("risk_rule_templates", *_columns())
    rules = _rules_table()

    # 1. New version of each changed rule, per company.
    for rule_id, note in CHANGE_NOTES.items():
        spec = specs[rule_id]
        bind.execute(
            sa.text(
                "UPDATE risk_rule_templates SET condition = :c, reason_template = :r, updated_at = :now "
                "WHERE rule_id = :id"
            ).bindparams(sa.bindparam("c", type_=postgresql.JSONB)),
            {"c": spec["condition"], "r": spec["reason_template"], "now": now, "id": rule_id},
        )
        current = bind.execute(
            sa.text(
                "SELECT DISTINCT ON (company_id) company_id, version, weight, severity, reason_template, "
                "is_active, category, check_type FROM risk_rules WHERE rule_id = :id "
                "ORDER BY company_id, version DESC"
            ),
            {"id": rule_id},
        ).mappings().all()
        rows = []
        for row in current:
            reason = row["reason_template"]
            if reason in (OLD_REASONS.get(rule_id), spec["reason_template"]):
                reason = spec["reason_template"]
            rows.append({
                "id": uuid.uuid4(),
                "created_at": now,
                "updated_at": now,
                "rule_id": rule_id,
                "category": row["category"],
                "check_type": row["check_type"],
                "condition": spec["condition"],
                "weight": row["weight"],
                "severity": row["severity"],
                "reason_template": reason,
                "is_active": row["is_active"],
                "company_id": row["company_id"],
                "version": row["version"] + 1,
                "effective_from": now,
                "change_note": note,
            })
        if rows:
            op.bulk_insert(rules, rows)

    # 2. New rules, version 1.
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
                        "effective_from": now, "change_note": "Default rule set (ghost content, scrubbed metadata)",
                    }
                    for company_id in companies
                ],
            )


def downgrade() -> None:
    # Non-destructive, like the earlier rule migrations: scored history
    # references every version row, so the new rules are only switched off
    # (and a Postgres enum value cannot be dropped).
    ids = ", ".join(f"'{r}'" for r in NEW_RULE_IDS)
    op.execute(f"UPDATE risk_rules SET is_active = false WHERE rule_id IN ({ids})")
    op.execute(f"UPDATE risk_rule_templates SET is_active = false WHERE rule_id IN ({ids})")
