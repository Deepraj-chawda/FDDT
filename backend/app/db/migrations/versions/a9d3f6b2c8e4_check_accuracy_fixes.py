"""check accuracy fixes: registry country, rule versions

Revision ID: a9d3f6b2c8e4
Revises: c4d8e2f6a9b1
Create Date: 2026-10-07 09:00:00.000000

1. `issuer_registry.country` (ISO alpha-2, nullable): lets issuer
   verification tell "not in the registry" from "the registry has nothing
   for this country/kind of issuer" (reported as not checked).
2. Risk rules — every change goes through the existing versioning, never an
   UPDATE of a rule row:
   - a NEW VERSION (current version + 1) of each rule whose check changed
     meaning (SEED_RULE_VERSIONS in app/services/risk_rule_seed.py), for
     every company. The company's own weight, severity and on/off state are
     carried over; the condition and (unless the company had reworded it)
     the reason text come from the new built-in definition. Assessments
     made before keep pointing at the old version rows, so no historical
     case is re-scored or reworded.
   - the new rules, version 1, for every company that lacks them.
   - the platform rule template (what new companies start from) is brought
     up to date for both.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.services.risk_rule_seed import SEED_RULES

revision: str = "a9d3f6b2c8e4"
down_revision: Union[str, None] = "c4d8e2f6a9b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_RULE_IDS = (
    "field.iban_trn_validation",
    "metadata.rescan_conflict",
    "field.period_quantity_mismatch",
)
CHANGE_NOTES = {
    "metadata.javascript_or_openaction": "v2: only JavaScript / launch / link-out / form-submit actions count; a view-setting OpenAction does not",
    "issuer.not_in_registry": "v2: an issuer is 'not checked' (not flagged) when the registry has no entries for its country or kind",
    "field.tax_rate_mismatch": "v2: tax on a subset of the line items (mixed-rate VAT) passes",
    "field.subtotal_line_item_mismatch": "v2: summed to the cent per line; lines marked PAID are never summed",
    "font.inconsistency_scanned": "v2: other amounts on a page are re-checked once several are flagged",
    "visual.alignment_inconsistency": "v2: findings about signatures/stamps or matching the scan's skew are not counted",
    "visual.sharpness_inconsistency": "v2: findings about signatures/stamps are not counted",
}
# The reason text each changed rule had at version 1 — a company still using
# it gets the new wording; one that reworded it keeps its own.
V1_REASONS = {
    "metadata.javascript_or_openaction": "'{document}' embeds JavaScript or an open-action — unusual for a business document.",
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
    bind = op.get_bind()
    now = datetime.now(timezone.utc)

    # ---- 1. registry country ------------------------------------------------
    op.add_column("issuer_registry", sa.Column("country", sa.String(length=2), nullable=True))

    # ---- 2. risk rules --------------------------------------------------------
    specs = {s["rule_id"]: s for s in SEED_RULES}
    templates = sa.table("risk_rule_templates", *_columns())
    rules = _rules_table()

    # 2a. New version of each changed rule, per company.
    # Its own list, not SEED_RULE_VERSIONS: later migrations version more rules.
    for rule_id in CHANGE_NOTES:
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
            if reason == V1_REASONS.get(rule_id, spec["reason_template"]):
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
                "change_note": CHANGE_NOTES[rule_id],
            })
        if rows:
            op.bulk_insert(rules, rows)

    # 2b. New rules, version 1.
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
                        "effective_from": now, "change_note": "Default rule set (check accuracy fixes)",
                    }
                    for company_id in companies
                ],
            )


def downgrade() -> None:
    # Non-destructive for rules, like the earlier rule migrations: scored
    # history references every version row, so the new rules are only
    # switched off and the new versions are left in place.
    ids = ", ".join(f"'{r}'" for r in NEW_RULE_IDS)
    op.execute(f"UPDATE risk_rules SET is_active = false WHERE rule_id IN ({ids})")
    op.execute(f"UPDATE risk_rule_templates SET is_active = false WHERE rule_id IN ({ids})")
    op.drop_column("issuer_registry", "country")
