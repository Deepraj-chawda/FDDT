"""bulk uploads: bulk_uploads (one per zip), bulk_upload_cases (one per case
folder), cases.bulk_upload_id + reference_label

Revision ID: d5a2f7c8e1b3
Revises: c9e3a1b5d7f4
Create Date: 2026-10-04 12:00:00.000000

Both new tables are tenant-owned. Each gets Row-Level Security with the usual
`tenant_isolation` policy for the app role and `platform_all` for the platform
role, plus explicit least-privilege grants (no DELETE for the app role: the
rows are the record of what was submitted).
"""
import os
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5a2f7c8e1b3"
down_revision: Union[str, None] = "c9e3a1b5d7f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_TABLES = ("bulk_uploads", "bulk_upload_cases")
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

    status_enum = postgresql.ENUM(
        "queued", "ingesting", "complete", "failed", name="bulk_upload_status"
    )
    status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "bulk_uploads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id"),
            nullable=False, index=True,
        ),
        sa.Column(
            "uploaded_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"),
            nullable=False, index=True,
        ),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column(
            "case_type", postgresql.ENUM(name="case_type", create_type=False), nullable=False
        ),
        sa.Column("blob_storage_path", sa.String(length=1024), nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("zip_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "status", postgresql.ENUM(name="bulk_upload_status", create_type=False), nullable=False
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("case_folder_count", sa.Integer(), nullable=True),
        sa.Column("cases_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cases_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("documents_accepted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("documents_rejected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("zip_details", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.add_column(
        "cases",
        sa.Column(
            "bulk_upload_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bulk_uploads.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_cases_bulk_upload_id", "cases", ["bulk_upload_id"])
    op.add_column("cases", sa.Column("reference_label", sa.String(length=255), nullable=True))

    op.create_table(
        "bulk_upload_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id"),
            nullable=False, index=True,
        ),
        sa.Column(
            "bulk_upload_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bulk_uploads.id"),
            nullable=False, index=True,
        ),
        sa.Column("folder_index", sa.Integer(), nullable=False),
        sa.Column("folder", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cases.id"), nullable=True),
        sa.Column("case_number", sa.String(length=64), nullable=True),
        sa.Column("files", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("bulk_upload_id", "folder_index"),
    )

    # ---- tenant isolation (the database half of the two layers) ----------
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} AS PERMISSIVE FOR ALL TO {app_role} "
            f"USING (company_id = fddt_current_company_id()) "
            f"WITH CHECK (company_id = fddt_current_company_id())"
        )
        op.execute(
            f"CREATE POLICY platform_all ON {table} AS PERMISSIVE FOR ALL TO {platform_role} "
            f"USING (true) WITH CHECK (true)"
        )
        op.execute(f"REVOKE ALL ON {table} FROM {app_role}")
        # No DELETE for the app role: these rows are the record of what was
        # submitted and what became of it.
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {app_role}")
        op.execute(f"REVOKE ALL ON {table} FROM {platform_role}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {platform_role}")


def downgrade() -> None:
    op.drop_table("bulk_upload_cases")
    op.drop_column("cases", "reference_label")
    op.drop_index("ix_cases_bulk_upload_id", table_name="cases")
    op.drop_column("cases", "bulk_upload_id")
    op.drop_table("bulk_uploads")
    postgresql.ENUM(name="bulk_upload_status").drop(op.get_bind(), checkfirst=True)
