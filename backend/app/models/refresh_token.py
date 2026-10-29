"""
`refresh_tokens` table — one row per refresh token ever issued
(app/services/refresh_tokens.py).

Only a SHA-256 hash of the token is stored: the token itself exists only in
the user's httpOnly cookie, so a database leak hands out no usable session.
Every refresh ROTATES: the presented row is revoked (`revoked_reason`
"rotated", `replaced_by_id` → its successor) and a new one is issued in the
same `family_id` (one family = one sign-in). Presenting an already-rotated
token again means it was copied — the whole family is revoked.

Platform-level (no company_id): read and written only through the platform
session, never by a company session.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, utcnow


class RefreshToken(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # users.token_version when issued: a bumped version ends the session.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    # When the sign-in itself must end (refresh_token_absolute_days).
    family_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # rotated | logout | reuse_detected | password_changed | revoked_all | account_changed
    revoked_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
