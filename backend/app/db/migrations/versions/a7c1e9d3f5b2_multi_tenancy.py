"""multi-tenancy: companies, company_id everywhere, Row-Level Security

Revision ID: a7c1e9d3f5b2
Revises: f2b6c1d84a90
Create Date: 2026-10-02 12:00:00.000000

Schema
- `companies` (id, name, is_active, created/updated_at).
- `company_usage_stats` (company_id, usage_date) daily usage counters for the
  billing dashboard, backfilled here from the existing data.
- `company_id` FK + index on every tenant-owned table. NOT NULL everywhere
  except `users` (NULL = platform admin, enforced by a CHECK constraint) and
  `audit_log` (NULL = platform-level event).
- `risk_rules` uniqueness becomes (company_id, rule_id, version); one
  `risk_settings` row per company.
- `user_role` gains `platform_admin`.

Data
- One company (DEFAULT_COMPANY_NAME, default "Default Company") is created and
  every existing row is assigned to it.
- Existing `admin` accounts become `platform_admin` with no company; every
  other account keeps its role, scoped to the default company.

Database roles + Row-Level Security (defense in depth — the application also
filters by company itself, see app/db/tenancy.py)
- `fddt_app` (DATABASE_APP_USER): LOGIN, NOSUPERUSER, NOBYPASSRLS. What the
  app uses for every company-scoped request and pipeline task.
- `fddt_platform` (DATABASE_PLATFORM_USER): LOGIN, NOBYPASSRLS. Only the
  explicit platform path (sign-in lookup, platform-admin screens, usage
  reconciliation). It sees every row through an explicit `platform_all`
  policy on each table (no BYPASSRLS, so no superuser is needed — see
  revision c9e3a1b5d7f4).
- RLS is enabled on every tenant-owned table with a policy for `fddt_app`:
  a row is visible/writable only when its company_id equals the
  `app.current_company_id` setting of the current transaction
  (`fddt_current_company_id()`). No setting = no rows.
- Grants are least-privilege and also enforce append-only/immutable tables at
  the database level: `audit_log`, `risk_rules`, `case_risk_assessments` and
  `case_reports` are SELECT + INSERT only for both app roles.

Any FUTURE tenant-owned table must get the same treatment in its own
migration: company_id column, ENABLE ROW LEVEL SECURITY, a `tenant_isolation`
policy and explicit grants (see `enable_tenant_rls` below) — there are
deliberately no default privileges, so a new table is unreachable by the app
roles until that is done.

Requires the migration to run as a role that owns the tables and may create
roles (CREATEROLE) — no superuser needed.
"""
import os
import re
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a7c1e9d3f5b2"
down_revision: Union[str, None] = "f2b6c1d84a90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Tenant tables whose company_id becomes NOT NULL.
REQUIRED_TENANT_TABLES = (
    "cases",
    "documents",
    "document_checks",
    "document_page_hashes",
    "cross_document_findings",
    "case_actions",
    "case_risk_assessments",
    "risk_scores",
    "risk_rules",
    "risk_settings",
    "issuer_registry",
    "signature_references",
    "signature_matches",
    "case_reports",
)
NULLABLE_TENANT_TABLES = ("users", "audit_log")
TENANT_TABLES = REQUIRED_TENANT_TABLES + NULLABLE_TENANT_TABLES

# Least-privilege grants for the RLS-enforced app role.
APP_GRANTS = {
    "cases": "SELECT, INSERT, UPDATE",
    "documents": "SELECT, INSERT, UPDATE",
    "document_checks": "SELECT, INSERT, UPDATE",
    "document_page_hashes": "SELECT, INSERT, UPDATE",
    "cross_document_findings": "SELECT, INSERT, UPDATE, DELETE",
    "case_actions": "SELECT, INSERT",
    "case_risk_assessments": "SELECT, INSERT",  # immutable scoring history
    "risk_scores": "SELECT",  # legacy, no longer written
    "risk_rules": "SELECT, INSERT",  # versioned: an edit INSERTs version + 1
    "risk_settings": "SELECT, INSERT, UPDATE",
    "issuer_registry": "SELECT, INSERT, UPDATE",  # soft delete only
    "signature_references": "SELECT, INSERT, UPDATE",
    "signature_matches": "SELECT, INSERT, UPDATE",
    "case_reports": "SELECT, INSERT",  # point-in-time records
    "audit_log": "SELECT, INSERT",  # append-only
    "users": "SELECT",  # user management is platform-only
    "companies": "SELECT",  # own company only (RLS)
    "company_usage_stats": "SELECT, INSERT, UPDATE",  # own counters (RLS)
}
# The platform role bypasses RLS but is still kept off UPDATE/DELETE of the
# append-only/immutable tables.
PLATFORM_IMMUTABLE = {"audit_log", "risk_rules", "case_risk_assessments", "case_reports"}
ALL_APP_TABLES = tuple(APP_GRANTS)

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _role(env_name: str, default: str) -> str:
    name = os.environ.get(env_name, default)
    if not _IDENT.match(name):
        raise ValueError(f"{env_name}={name!r} is not a plain lowercase role name")
    return name


def _settings():
    from app.core.config import settings

    return settings


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def enable_tenant_rls(
    table: str, app_role: str, key_column: str = "company_id", platform_role: str | None = None
) -> None:
    """ENABLE RLS on `table` with the tenant policy for the app role and the
    full-access `platform_all` policy for the platform role."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} AS PERMISSIVE FOR ALL TO {app_role} "
        f"USING ({key_column} = fddt_current_company_id()) "
        f"WITH CHECK ({key_column} = fddt_current_company_id())"
    )
    if platform_role:
        op.execute(f"DROP POLICY IF EXISTS platform_all ON {table}")
        op.execute(
            f"CREATE POLICY platform_all ON {table} AS PERMISSIVE FOR ALL TO {platform_role} "
            f"USING (true) WITH CHECK (true)"
        )


def upgrade() -> None:
    settings = _settings()
    app_role = _role("DATABASE_APP_USER", settings.database_app_user)
    platform_role = _role("DATABASE_PLATFORM_USER", settings.database_platform_user)

    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'platform_admin'")

    # ---- companies + usage counters ---------------------------------------
    op.create_table(
        "companies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_table(
        "company_usage_stats",
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id"), primary_key=True),
        sa.Column("usage_date", sa.Date(), primary_key=True),
        sa.Column("cases_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("documents_uploaded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("files_stored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    default_company_id = str(uuid.uuid4())
    default_name = os.environ.get("DEFAULT_COMPANY_NAME", settings.default_company_name)
    op.execute(
        f"INSERT INTO companies (id, name, is_active) VALUES ('{default_company_id}', {_literal(default_name)}, true)"
    )

    # ---- company_id on every tenant table ---------------------------------
    for table in TENANT_TABLES:
        op.add_column(table, sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=True))
        op.create_foreign_key(f"fk_{table}_company_id", table, "companies", ["company_id"], ["id"])
        op.create_index(f"ix_{table}_company_id", table, ["company_id"])

    for table in REQUIRED_TENANT_TABLES:
        op.execute(f"UPDATE {table} SET company_id = '{default_company_id}'")
        op.alter_column(table, "company_id", nullable=False)

    # Everything that existed belonged to the single tenant.
    op.execute(f"UPDATE audit_log SET company_id = '{default_company_id}'")

    # Users: the old admins become platform admins (no company); everyone
    # else keeps their role inside the default company.
    op.execute(
        f"UPDATE users SET company_id = '{default_company_id}' WHERE role::text <> 'admin'"
    )
    op.execute("UPDATE users SET role = 'platform_admin', company_id = NULL WHERE role::text = 'admin'")
    op.create_check_constraint(
        "ck_users_platform_admin_has_no_company",
        "users",
        "(role = 'platform_admin') = (company_id IS NULL)",
    )

    # ---- per-company uniqueness --------------------------------------------
    op.drop_constraint("uq_risk_rules_rule_id_version", "risk_rules", type_="unique")
    op.create_unique_constraint(
        "uq_risk_rules_company_rule_id_version", "risk_rules", ["company_id", "rule_id", "version"]
    )
    op.create_unique_constraint("uq_risk_settings_company_id", "risk_settings", ["company_id"])

    # ---- backfill usage counters (UTC days) ---------------------------------
    op.execute(
        """
        INSERT INTO company_usage_stats
            (company_id, usage_date, cases_created, documents_uploaded, files_stored, storage_bytes, updated_at)
        SELECT company_id, d, SUM(c), SUM(docs), SUM(files), SUM(bytes), now()
        FROM (
            SELECT company_id, (created_at AT TIME ZONE 'UTC')::date AS d,
                   COUNT(*) AS c, 0 AS docs, 0 AS files, 0::bigint AS bytes
              FROM cases GROUP BY 1, 2
            UNION ALL
            SELECT company_id, (created_at AT TIME ZONE 'UTC')::date,
                   0, COUNT(*), COUNT(*), COALESCE(SUM(file_size_bytes), 0)
              FROM documents GROUP BY 1, 2
            UNION ALL
            SELECT company_id, (generated_at AT TIME ZONE 'UTC')::date,
                   0, 0, COUNT(*), COALESCE(SUM(file_size_bytes), 0)
              FROM case_reports GROUP BY 1, 2
        ) per_day
        GROUP BY company_id, d
        """
    )

    # ---- database roles ------------------------------------------------------
    app_password = os.environ.get("DATABASE_APP_PASSWORD", settings.database_app_password)
    platform_password = os.environ.get("DATABASE_PLATFORM_PASSWORD", settings.database_platform_password)
    # Attributes go on CREATE ROLE only: PostgreSQL 16 requires superuser to
    # even mention SUPERUSER/BYPASSRLS in ALTER ROLE, while a CREATEROLE
    # owner may create a role with them switched off. ALTER only sets the
    # password, then the roles are checked to really be unprivileged.
    for role in (app_role, platform_role):
        op.execute(
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') "
            f"THEN CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; "
            f"END IF; END $$"
        )
    op.execute(f"ALTER ROLE {app_role} WITH LOGIN PASSWORD {_literal(app_password)}")
    op.execute(f"ALTER ROLE {platform_role} WITH LOGIN PASSWORD {_literal(platform_password)}")
    op.execute(
        f"""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{app_role}' AND (rolsuper OR rolbypassrls)) THEN
            RAISE EXCEPTION 'role {app_role} must be NOSUPERUSER NOBYPASSRLS for Row-Level Security to apply';
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{platform_role}' AND rolsuper) THEN
            RAISE EXCEPTION 'role {platform_role} must not be a superuser';
          END IF;
        END $$
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {app_role}, {platform_role}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {app_role}, {platform_role}")

    for table, privileges in APP_GRANTS.items():
        op.execute(f"REVOKE ALL ON {table} FROM {app_role}")
        op.execute(f"GRANT {privileges} ON {table} TO {app_role}")
        op.execute(f"REVOKE ALL ON {table} FROM {platform_role}")
        platform_privileges = (
            "SELECT, INSERT" if table in PLATFORM_IMMUTABLE else "SELECT, INSERT, UPDATE, DELETE"
        )
        op.execute(f"GRANT {platform_privileges} ON {table} TO {platform_role}")

    # ---- Row-Level Security ------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fddt_current_company_id() RETURNS uuid
        LANGUAGE sql STABLE AS
        $$ SELECT NULLIF(current_setting('app.current_company_id', true), '')::uuid $$
        """
    )
    op.execute(f"GRANT EXECUTE ON FUNCTION fddt_current_company_id() TO {app_role}, {platform_role}")
    for table in TENANT_TABLES:
        enable_tenant_rls(table, app_role, platform_role=platform_role)
    enable_tenant_rls("company_usage_stats", app_role, platform_role=platform_role)
    enable_tenant_rls("companies", app_role, key_column="id", platform_role=platform_role)


def downgrade() -> None:
    settings = _settings()
    app_role = _role("DATABASE_APP_USER", settings.database_app_user)
    platform_role = _role("DATABASE_PLATFORM_USER", settings.database_platform_user)

    for table in TENANT_TABLES + ("company_usage_stats", "companies"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"DROP POLICY IF EXISTS platform_all ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    for table in ALL_APP_TABLES:
        op.execute(f"REVOKE ALL ON {table} FROM {app_role}, {platform_role}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {app_role}, {platform_role}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {app_role}, {platform_role}")
    op.execute("DROP FUNCTION IF EXISTS fddt_current_company_id()")
    op.execute(f"DROP ROLE IF EXISTS {app_role}")
    op.execute(f"DROP ROLE IF EXISTS {platform_role}")

    op.drop_constraint("uq_risk_settings_company_id", "risk_settings", type_="unique")
    op.drop_constraint("uq_risk_rules_company_rule_id_version", "risk_rules", type_="unique")
    op.drop_constraint("ck_users_platform_admin_has_no_company", "users", type_="check")
    # Collapsing companies is lossy: keep only the oldest company's config so
    # the single-tenant uniqueness constraints can be restored.
    op.execute(
        "DELETE FROM risk_rules WHERE company_id <> (SELECT id FROM companies ORDER BY created_at LIMIT 1)"
    )
    op.execute(
        "DELETE FROM risk_settings WHERE company_id <> (SELECT id FROM companies ORDER BY created_at LIMIT 1)"
    )
    op.create_unique_constraint("uq_risk_rules_rule_id_version", "risk_rules", ["rule_id", "version"])

    for table in TENANT_TABLES:
        op.drop_index(f"ix_{table}_company_id", table_name=table)
        op.drop_constraint(f"fk_{table}_company_id", table, type_="foreignkey")
        op.drop_column(table, "company_id")
    op.drop_table("company_usage_stats")
    op.drop_table("companies")

    # Postgres can't drop an enum value: rebuild user_role without it.
    op.execute("ALTER TYPE user_role RENAME TO user_role_old")
    op.execute("CREATE TYPE user_role AS ENUM ('user', 'reviewer_l1', 'reviewer_l2', 'admin')")
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE user_role USING ("
        "CASE WHEN role::text = 'platform_admin' THEN 'admin' ELSE role::text END)::user_role"
    )
    op.execute("DROP TYPE user_role_old")
