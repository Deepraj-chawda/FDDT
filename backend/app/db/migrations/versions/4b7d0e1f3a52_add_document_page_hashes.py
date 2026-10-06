"""add document_page_hashes table

Revision ID: 4b7d0e1f3a52
Revises: 9c2f4a7b1e08
Create Date: 2026-09-16 09:00:00.000000

Adds the storage needed for duplicate/near-duplicate detection (SPECIFICATION.md
section 3.2 — see app/services/forensics/duplicate_check.py and
app/tasks/document_checks_duplicate_task.py): one row per rendered PDF
page's perceptual hash. A dedicated table rather than a column on
`documents` since a document can be multi-page and each page needs its
own hash to compare against prior documents' pages individually.

`document_check_type` already has a `duplicate_detection` value (added in
the initial schema, e7ce49024f9e) — nothing to change there.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "4b7d0e1f3a52"
down_revision: Union[str, None] = "9c2f4a7b1e08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_page_hashes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("phash", sa.String(32), nullable=False),
    )
    op.create_index(
        "ix_document_page_hashes_document_id", "document_page_hashes", ["document_id"]
    )
    op.create_index("ix_document_page_hashes_phash", "document_page_hashes", ["phash"])


def downgrade() -> None:
    op.drop_index("ix_document_page_hashes_phash", table_name="document_page_hashes")
    op.drop_index("ix_document_page_hashes_document_id", table_name="document_page_hashes")
    op.drop_table("document_page_hashes")
