"""add case_type to cases

Revision ID: 3cf997a5cb00
Revises: e7ce49024f9e
Create Date: 2026-09-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "3cf997a5cb00"
down_revision: Union[str, None] = "e7ce49024f9e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CASE_TYPE_VALUES = (
    "school_document",
    "vendor_invoice",
    "commercial_invoice",
    "procurement_documentation",
    "quotation",
    "travel_reimbursement",
    "other",
)


def upgrade() -> None:
    case_type_enum = sa.Enum(*CASE_TYPE_VALUES, name="case_type")
    case_type_enum.create(op.get_bind(), checkfirst=True)
    op.add_column("cases", sa.Column("case_type", case_type_enum, nullable=False))


def downgrade() -> None:
    op.drop_column("cases", "case_type")
    sa.Enum(name="case_type").drop(op.get_bind(), checkfirst=True)
