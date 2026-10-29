"""platform admins' audit rows are platform-only

Revision ID: b3e6d9a2f4c7
Revises: a2d6f9c3e7b4
Create Date: 2026-10-26 12:00:00.000000

Nothing a platform admin does may be visible to a company. Until now, the
rows for a platform admin's user management (`user_created`, `user_updated`,
`user_password_reset`) and for their edits to a company's issuer registry or
risk rules were written with that company's id, so the company's Audit
History showed them (as "Platform team"). New rows are written platform-only
(company_id NULL, the company in event_data.company_id — the same shape as
`platform_admin_access` since revision c9e3a1b5d7f4); this moves the existing
ones there too, tagging them with the admin's identity.

Downgrade does not move them back: re-exposing them to companies is exactly
what this revision exists to prevent.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b3e6d9a2f4c7"
down_revision: Union[str, None] = "a2d6f9c3e7b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE audit_log AS a
        SET company_id = NULL,
            event_data = jsonb_build_object(
                'company_id', a.company_id::text,
                'company_name', c.name,
                'admin_email', u.email,
                'admin_name', u.full_name
            ) || COALESCE(a.event_data, '{}'::jsonb) || '{"platform_admin": true}'::jsonb
        FROM users AS u, companies AS c
        WHERE a.actor_user_id = u.id
          AND u.role = 'platform_admin'
          AND a.company_id IS NOT NULL
          AND c.id = a.company_id
        """
    )


def downgrade() -> None:
    pass
