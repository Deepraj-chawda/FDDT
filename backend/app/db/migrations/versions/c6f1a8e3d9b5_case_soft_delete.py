"""case soft delete (platform admins)

Revision ID: c6f1a8e3d9b5
Revises: b3e6d9a2f4c7
Create Date: 2026-10-26 15:00:00.000000

`cases.deleted_at` / `cases.deleted_by_user_id`: a platform admin's soft
delete. A deleted case is invisible to every company session (filtered by the
application tenancy layer, app/db/tenancy.py); its rows and files are kept so
it can be restored.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6f1a8e3d9b5"
down_revision: Union[str, None] = "b3e6d9a2f4c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "cases",
        sa.Column("deleted_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
    )
    op.create_index("ix_cases_deleted_at", "cases", ["deleted_at"])


def downgrade() -> None:
    op.drop_index("ix_cases_deleted_at", table_name="cases")
    op.drop_column("cases", "deleted_by_user_id")
    op.drop_column("cases", "deleted_at")
