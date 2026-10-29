"""refresh tokens + token versions (production sessions)

Revision ID: d7a2c4e9f1b6
Revises: c6f1a8e3d9b5
Create Date: 2026-10-26 18:00:00.000000

  - `users.token_version`: carried in every access token (`ver`); bumped to
    invalidate every token of an account at once.
  - `refresh_tokens`: hashed, rotating refresh tokens (app/services/
    refresh_tokens.py). Platform-level: granted to the platform role only —
    the company-scoped app role has no access at all.

Existing access tokens (no `ver`/`iss`/`aud`) stop working: everyone signs in
once more after this deploys.
"""
import os
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7a2c4e9f1b6"
down_revision: Union[str, None] = "c6f1a8e3d9b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _role(env_name: str, default: str) -> str:
    from app.core.config import settings

    name = os.environ.get(env_name, getattr(settings, default))
    if not _IDENT.match(name):
        raise ValueError(f"{env_name}={name!r} is not a plain lowercase role name")
    return name


def upgrade() -> None:
    op.add_column("users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))

    op.create_table(
        "refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("family_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=32), nullable=True),
        sa.Column("replaced_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_index("ix_refresh_tokens_expires_at", "refresh_tokens", ["expires_at"])

    platform_role = _role("DATABASE_PLATFORM_USER", "database_platform_user")
    app_role = _role("DATABASE_APP_USER", "database_app_user")
    op.execute(f"REVOKE ALL ON refresh_tokens FROM {app_role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON refresh_tokens TO {platform_role}")


def downgrade() -> None:
    op.drop_table("refresh_tokens")
    op.drop_column("users", "token_version")
