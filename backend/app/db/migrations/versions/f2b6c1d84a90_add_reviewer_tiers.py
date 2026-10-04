"""split the reviewer role into L1/L2 tiers and route escalations to L2

Revision ID: f2b6c1d84a90
Revises: e3a91d7c5b20
Create Date: 2026-09-26 12:00:00.000000

- `user_role`: `reviewer` is renamed to `reviewer_l1` (every existing reviewer
  keeps exactly the permissions they had) and `reviewer_l2` is added.
- `cases.priority` (normal|escalated) is replaced by `cases.assigned_tier`
  (l1|l2): escalation now hands the case to the L2 tier instead of only
  flagging it. Previously escalated cases become `l2`.
- `case_actions.actor_role`: the actor's role at the time of the action, so
  the history keeps reading "escalated by Reviewer L1" after a promotion.
  Existing rows are backfilled from the actor's current (renamed) role.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f2b6c1d84a90"
down_revision: Union[str, None] = "e3a91d7c5b20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE can't run inside a transaction block on older
    # Postgres versions, and a new value can't be used in the transaction
    # that added it — so step out of Alembic's transaction for it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE user_role RENAME VALUE 'reviewer' TO 'reviewer_l1'")
        op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'reviewer_l2' AFTER 'reviewer_l1'")

    # ---- cases.priority -> cases.assigned_tier ----------------------------
    case_tier = postgresql.ENUM("l1", "l2", name="case_tier")
    case_tier.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "cases",
        sa.Column(
            "assigned_tier",
            postgresql.ENUM("l1", "l2", name="case_tier", create_type=False),
            nullable=False,
            server_default="l1",
        ),
    )
    op.execute("UPDATE cases SET assigned_tier = 'l2' WHERE priority = 'escalated'")
    op.drop_column("cases", "priority")
    op.execute("DROP TYPE IF EXISTS case_priority")

    # ---- case_actions.actor_role ------------------------------------------
    op.add_column("case_actions", sa.Column("actor_role", sa.String(length=32), nullable=True))
    op.execute(
        "UPDATE case_actions SET actor_role = users.role::text "
        "FROM users WHERE users.id = case_actions.actor_user_id"
    )


def downgrade() -> None:
    op.drop_column("case_actions", "actor_role")

    case_priority = postgresql.ENUM("normal", "escalated", name="case_priority")
    case_priority.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "cases",
        sa.Column(
            "priority",
            postgresql.ENUM("normal", "escalated", name="case_priority", create_type=False),
            nullable=False,
            server_default="normal",
        ),
    )
    op.execute("UPDATE cases SET priority = 'escalated' WHERE assigned_tier = 'l2'")
    op.drop_column("cases", "assigned_tier")
    op.execute("DROP TYPE IF EXISTS case_tier")

    # Postgres can't drop an enum value: fold L2 into the single reviewer
    # role and rebuild the type.
    op.execute("ALTER TYPE user_role RENAME TO user_role_old")
    op.execute("CREATE TYPE user_role AS ENUM ('user', 'reviewer', 'admin')")
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE user_role USING ("
        "CASE WHEN role::text IN ('reviewer_l1', 'reviewer_l2') THEN 'reviewer' "
        "ELSE role::text END)::user_role"
    )
    op.execute("DROP TYPE user_role_old")
