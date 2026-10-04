"""add case_reports table

Revision ID: c8e2a5f19b04
Revises: d4f8b2a61c37
Create Date: 2026-09-20 13:00:00.000000

History of generated per-case PDF reports (app/services/
case_report_service.py). Insert-only: a report reflects the risk-rule
versions active when it was generated, so earlier reports are kept rather
than overwritten. The PDFs themselves live in Blob Storage under the
`reports/` prefix, separate from the original case documents.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c8e2a5f19b04"
down_revision: Union[str, None] = "d4f8b2a61c37"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "case_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cases.id"), nullable=False
        ),
        sa.Column(
            "generated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blob_url", sa.Text(), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("report_sha256", sa.String(64), nullable=False),
        sa.Column(
            "risk_assessment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("case_risk_assessments.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_case_reports_case_id", "case_reports", ["case_id"])
    op.create_index("ix_case_reports_generated_at", "case_reports", ["generated_at"])


def downgrade() -> None:
    op.drop_index("ix_case_reports_generated_at", table_name="case_reports")
    op.drop_index("ix_case_reports_case_id", table_name="case_reports")
    op.drop_table("case_reports")
