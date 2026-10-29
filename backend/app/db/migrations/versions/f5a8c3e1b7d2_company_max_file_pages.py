"""companies.max_file_pages

Revision ID: f5a8c3e1b7d2
Revises: e1f4b7a2c9d3
Create Date: 2026-10-28 14:00:00.000000

Per-company page cap for uploaded PDFs, set by a platform admin like the
size limits. Existing companies get 100; new ones get DEFAULT_MAX_FILE_PAGES
(app/models/company.py), so no server default is kept.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5a8c3e1b7d2"
down_revision: Union[str, None] = "e1f4b7a2c9d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("companies", sa.Column("max_file_pages", sa.Integer(), nullable=False, server_default="100"))
    op.alter_column("companies", "max_file_pages", server_default=None)
    op.create_check_constraint("ck_companies_max_file_pages_positive", "companies", "max_file_pages > 0")


def downgrade() -> None:
    op.drop_constraint("ck_companies_max_file_pages_positive", "companies", type_="check")
    op.drop_column("companies", "max_file_pages")
