"""add visual_inconsistency_review check type

Revision ID: b1f4a8d29e6c
Revises: 4b7d0e1f3a52
Create Date: 2026-09-17 00:00:00.000000

Adds `visual_inconsistency_review` to `document_check_type` — the
combined vision-model check (SPECIFICATION.md sections 3.2/3.4/3.5, see
app/services/visual_inconsistency_service.py's module docstring for why
this covers BOTH the visual-inconsistency review and the AI-generated-
content question in one check_type rather than two: Azure AI Content
Safety, the API SPECIFICATION.md section 2 names for AI-generated-content
detection, turns out to have no synthetic-content detection capability
at all).

The pre-existing `ai_content_detection` enum value is left in place
(unused, same as before this migration) rather than dropped — Postgres
enum values can't be removed without a full type rebuild, and it costs
nothing to leave it as dead/unused, same tradeoff already made for
`issuer_validation` in 7a3c9e0f6d21.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1f4a8d29e6c"
down_revision: Union[str, None] = "4b7d0e1f3a52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE document_check_type ADD VALUE IF NOT EXISTS 'visual_inconsistency_review'"
    )


def downgrade() -> None:
    # Postgres has no "remove enum value" short of a full type rebuild —
    # same tradeoff as every other additive enum-value migration in this
    # project (see 7a3c9e0f6d21). Left as an unused value on downgrade.
    pass
