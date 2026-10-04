"""production hardening: policy-based platform access, platform-only access
audit, risk-rule templates, one row per check

Revision ID: c9e3a1b5d7f4
Revises: a7c1e9d3f5b2
Create Date: 2026-10-03 10:30:00.000000

1. No BYPASSRLS anywhere. `fddt_platform` loses BYPASSRLS; instead every
   RLS-enabled table gets an explicit permissive policy
   `platform_all ... TO fddt_platform USING (true) WITH CHECK (true)`.
   Creating a policy only needs table ownership — no superuser — so this
   works on managed PostgreSQL (Azure Flexible Server) as well. Revoking an
   existing BYPASSRLS does need superuser, so it is only attempted when the
   role actually has it.
2. `platform_admin_access` audit rows move to company_id = NULL (the target
   company stays in event_data.company_id). NULL-company rows are invisible
   to every company session at the database level, so no company user can
   ever see that platform access happened; platform admins still see every
   row. (A one-time relocation of a column value on existing rows; no row is
   removed and no content changes.)
3. `risk_rule_templates` (platform-level default rule set), seeded from the
   CURRENT version of every rule of the oldest company (the migrated
   "Default Company").
4. Exactly one row per (document, check_type), per (document, page) hash,
   per (case, evidence) assessment and per (reference, document, scope)
   signature match. Duplicates left by earlier task re-runs are collapsed
   first: the newest check/hash row is kept; for assessments the one a
   report points at (else the earliest) is kept.
"""
import os
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c9e3a1b5d7f4"
down_revision: Union[str, None] = "a7c1e9d3f5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RLS_TABLES = (
    "cases", "documents", "document_checks", "document_page_hashes", "cross_document_findings",
    "case_actions", "case_risk_assessments", "risk_scores", "risk_rules", "risk_settings",
    "issuer_registry", "signature_references", "signature_matches", "case_reports", "users",
    "audit_log", "company_usage_stats", "companies",
)
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _role(env_name: str, default: str) -> str:
    from app.core.config import settings

    name = os.environ.get(env_name, getattr(settings, default))
    if not _IDENT.match(name):
        raise ValueError(f"{env_name}={name!r} is not a plain lowercase role name")
    return name


def upgrade() -> None:
    platform_role = _role("DATABASE_PLATFORM_USER", "database_platform_user")
    app_role = _role("DATABASE_APP_USER", "database_app_user")

    # ---- 1. policy-based platform access, no BYPASSRLS -------------------
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS platform_all ON {table}")
        op.execute(
            f"CREATE POLICY platform_all ON {table} AS PERMISSIVE FOR ALL TO {platform_role} "
            f"USING (true) WITH CHECK (true)"
        )
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{platform_role}' AND rolbypassrls) THEN
            EXECUTE 'ALTER ROLE {platform_role} NOBYPASSRLS';
          END IF;
        END $$
        """
    )

    # ---- 2. platform-admin access rows: platform-only ---------------------
    op.execute("UPDATE audit_log SET company_id = NULL WHERE event_type = 'platform_admin_access'")

    # ---- 3. risk rule templates ------------------------------------------
    op.create_table(
        "risk_rule_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("rule_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("check_type", sa.String(length=64), nullable=False),
        sa.Column("condition", postgresql.JSONB(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("reason_template", sa.String(length=1024), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.execute(
        """
        INSERT INTO risk_rule_templates
            (id, rule_id, category, check_type, condition, weight, severity, reason_template, is_active)
        SELECT gen_random_uuid(), r.rule_id, r.category, r.check_type, r.condition, r.weight, r.severity,
               r.reason_template, r.is_active
        FROM risk_rules r
        JOIN (
            SELECT rule_id, max(version) AS version FROM risk_rules
            WHERE company_id = (SELECT id FROM companies ORDER BY created_at LIMIT 1)
            GROUP BY rule_id
        ) cur ON cur.rule_id = r.rule_id AND cur.version = r.version
        WHERE r.company_id = (SELECT id FROM companies ORDER BY created_at LIMIT 1)
        """
    )
    # Platform-level table: only the platform role touches it (no RLS needed,
    # no grant for the company-scoped app role at all).
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON risk_rule_templates TO {platform_role}")
    op.execute(f"REVOKE ALL ON risk_rule_templates FROM {app_role}")

    # ---- 4. one row per check / hash / assessment / match ----------------
    op.execute(
        """
        DELETE FROM document_checks d
        USING document_checks newer
        WHERE d.document_id = newer.document_id AND d.check_type = newer.check_type
          AND (newer.created_at, newer.id) > (d.created_at, d.id)
        """
    )
    op.create_unique_constraint(
        "uq_document_checks_document_check_type", "document_checks", ["document_id", "check_type"]
    )
    op.execute(
        """
        DELETE FROM document_page_hashes d
        USING document_page_hashes newer
        WHERE d.document_id = newer.document_id AND d.page_number = newer.page_number
          AND (newer.created_at, newer.id) > (d.created_at, d.id)
        """
    )
    op.create_unique_constraint(
        "uq_document_page_hashes_document_page", "document_page_hashes", ["document_id", "page_number"]
    )
    op.execute(
        """
        WITH ranked AS (
            SELECT a.id,
                   row_number() OVER (
                       PARTITION BY a.case_id, a.evidence_fingerprint
                       ORDER BY (EXISTS (SELECT 1 FROM case_reports r WHERE r.risk_assessment_id = a.id)) DESC,
                                a.computed_at, a.id
                   ) AS rn
            FROM case_risk_assessments a
        )
        DELETE FROM case_risk_assessments a USING ranked
        WHERE a.id = ranked.id AND ranked.rn > 1
        """
    )
    op.create_unique_constraint(
        "uq_case_risk_assessments_case_evidence", "case_risk_assessments", ["case_id", "evidence_fingerprint"]
    )
    op.execute(
        """
        DELETE FROM signature_matches d
        USING signature_matches newer
        WHERE d.signature_reference_id = newer.signature_reference_id AND d.document_id = newer.document_id
          AND d.comparison_scope = newer.comparison_scope
          AND (newer.compared_at, newer.id) > (d.compared_at, d.id)
        """
    )
    op.create_unique_constraint(
        "uq_signature_matches_ref_doc_scope",
        "signature_matches",
        ["signature_reference_id", "document_id", "comparison_scope"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_signature_matches_ref_doc_scope", "signature_matches", type_="unique")
    op.drop_constraint("uq_case_risk_assessments_case_evidence", "case_risk_assessments", type_="unique")
    op.drop_constraint("uq_document_page_hashes_document_page", "document_page_hashes", type_="unique")
    op.drop_constraint("uq_document_checks_document_check_type", "document_checks", type_="unique")
    op.drop_table("risk_rule_templates")
    op.execute(
        "UPDATE audit_log SET company_id = (event_data->>'company_id')::uuid "
        "WHERE event_type = 'platform_admin_access' AND event_data ? 'company_id'"
    )
    # The platform_all policies stay: revision a7c1e9d3f5b2 now creates them
    # itself (fresh installs never had BYPASSRLS), so they belong to it.
