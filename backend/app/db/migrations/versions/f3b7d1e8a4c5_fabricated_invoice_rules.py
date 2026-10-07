"""fabricated-invoice rules: fake scan line, typed stamp, TRN, multi-invoice, web origin

Revision ID: f3b7d1e8a4c5
Revises: e1a4c6b9d2f3
Create Date: 2026-10-07 18:30:00.000000

From CASE-7AC9B30F (an invoice built as a web page and printed to PDF). Same
versioning as b7e2d4f1c9a3 — never an UPDATE of a rule row:
  - NEW VERSIONS for every company, whose check now behaves differently:
      field.tax_rate_mismatch v3 — a rate above 0 with no tax on a non-zero
        base is flagged; the rate is read from the printed label ("VAT (5%)")
        when it was not extracted;
      duplicate.cross_case_match v2 — a page match with different invoice
        number / date / amount / student is "same template", not scored;
      metadata.orphaned_objects v3 — empty streams are not counted.
  - new rules, version 1: field.fake_scan_watermark, signature.synthetic_stamp,
    signature.synthetic_stamp_unsigned, field.tax_invoice_without_trn,
    field.multiple_invoices_same_period, field.per_invoice_mismatch,
    metadata.web_page_origin.
"""
import uuid
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.services.risk_rule_seed import SEED_RULES

revision: str = "f3b7d1e8a4c5"
down_revision: Union[str, None] = "e1a4c6b9d2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_RULE_IDS = (
    "field.fake_scan_watermark", "signature.synthetic_stamp", "signature.synthetic_stamp_unsigned",
    "field.tax_invoice_without_trn", "field.multiple_invoices_same_period", "field.per_invoice_mismatch",
    "metadata.web_page_origin",
)
CHANGE_NOTES = {
    "field.tax_rate_mismatch": (
        "v3: a rate above 0 with no tax on a non-zero base is flagged; the printed label (VAT (5%)) is read "
        "when the rate was not extracted"
    ),
    "duplicate.cross_case_match": (
        "v2: page matches whose invoice number, date, amount or student differ are 'same template', not scored"
    ),
    "metadata.orphaned_objects": "v3: empty streams (PDFium's empty Form XObjects) are not counted",
}
# The reason wording is unchanged for these rules.
OLD_REASONS: dict[str, str] = {}


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
                        "effective_from": now, "change_note": "Default rule set (fabricated web-page invoices)",
                    }
                    for company_id in companies
                ],
            )


def downgrade() -> None:
    # Non-destructive, like the earlier rule migrations: scored history
    # references every version row, so the new rules are only switched off.
    ids = ", ".join(f"'{r}'" for r in NEW_RULE_IDS)
    op.execute(f"UPDATE risk_rules SET is_active = false WHERE rule_id IN ({ids})")
    op.execute(f"UPDATE risk_rule_templates SET is_active = false WHERE rule_id IN ({ids})")
