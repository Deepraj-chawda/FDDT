"""per-company upload limits: companies.max_file_size_mb, max_zip_size_mb

Revision ID: e8b4c2d9f6a1
Revises: d5a2f7c8e1b3
Create Date: 2026-10-04 16:00:00.000000

Existing companies are backfilled with today's limits (10 MB per file,
300 MB per zip). The server defaults are then dropped. The application
stores a NEW company's limits explicitly at creation time (from
DEFAULT_MAX_FILE_SIZE_MB / DEFAULT_MAX_ZIP_SIZE_MB), so a later change to
those defaults never silently changes an existing company.

No grant changes: the app role already reads only its own company row
(RLS on `companies`) and never writes it. Only the platform role, which
already has UPDATE, edits the limits.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8b4c2d9f6a1"
down_revision: Union[str, None] = "d5a2f7c8e1b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("max_file_size_mb", sa.Integer(), nullable=False, server_default="10"))
    op.add_column("companies", sa.Column("max_zip_size_mb", sa.Integer(), nullable=False, server_default="300"))
    op.alter_column("companies", "max_file_size_mb", server_default=None)
    op.alter_column("companies", "max_zip_size_mb", server_default=None)
    op.create_check_constraint("ck_companies_max_file_size_mb_positive", "companies", "max_file_size_mb > 0")
    op.create_check_constraint("ck_companies_max_zip_size_mb_positive", "companies", "max_zip_size_mb > 0")


def downgrade() -> None:
    op.drop_constraint("ck_companies_max_zip_size_mb_positive", "companies", type_="check")
    op.drop_constraint("ck_companies_max_file_size_mb_positive", "companies", type_="check")
    op.drop_column("companies", "max_zip_size_mb")
    op.drop_column("companies", "max_file_size_mb")
