"""documents.file_size_bytes as BIGINT

Revision ID: e1f4b7a2c9d3
Revises: d7a2c4e9f1b6
Create Date: 2026-10-28 12:00:00.000000

The per-company file-size limit has no upper bound, so a document over
2 GiB would overflow a 32-bit INTEGER on insert.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1f4b7a2c9d3"
down_revision: Union[str, None] = "d7a2c4e9f1b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "documents", "file_size_bytes", type_=sa.BigInteger(), existing_type=sa.Integer(), existing_nullable=True
    )


def downgrade() -> None:
    op.alter_column(
        "documents", "file_size_bytes", type_=sa.Integer(), existing_type=sa.BigInteger(), existing_nullable=True
    )
