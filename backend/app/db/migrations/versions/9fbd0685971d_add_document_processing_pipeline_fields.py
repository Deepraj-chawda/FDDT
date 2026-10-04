"""add document processing pipeline fields

Revision ID: 9fbd0685971d
Revises: 3cf997a5cb00
Create Date: 2026-09-12 17:44:14.464750

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '9fbd0685971d'
down_revision: Union[str, None] = '3cf997a5cb00'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PROCESSING_STATUS_VALUES = ("pending", "processing", "complete", "failed")


def upgrade() -> None:
    # NOTE: autogenerate also proposed dropping/recreating unrelated unique
    # constraints/indexes on cases.case_number, risk_rules.rule_id, and
    # users.email (a pre-existing unique=True+index=True SQLAlchemy
    # representation quirk, not something this change touches) — left out
    # deliberately.
    status_enum = postgresql.ENUM(*PROCESSING_STATUS_VALUES, name="document_processing_status")
    status_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "documents",
        sa.Column(
            "processing_status",
            status_enum,
            nullable=False,
            server_default="pending",
        ),
    )
    op.alter_column("documents", "processing_status", server_default=None)
    op.add_column(
        "documents",
        sa.Column("extracted_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("documents", sa.Column("ocr_text", sa.Text(), nullable=True))
    op.add_column("documents", sa.Column("processing_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "processing_error")
    op.drop_column("documents", "ocr_text")
    op.drop_column("documents", "extracted_fields")
    op.drop_column("documents", "processing_status")
    postgresql.ENUM(name="document_processing_status").drop(op.get_bind(), checkfirst=True)
