"""
Refresh tokens: how a sign-in outlives one short-lived access token.

* Opaque, 256-bit random tokens (`secrets.token_urlsafe(32)`), handed to the
  browser ONLY as an httpOnly, Secure, SameSite=Strict cookie scoped to the
  auth routes (app/api/auth.py) — page scripts can never read them. Only the
  SHA-256 hash is stored (app/models/refresh_token.py).
* Rotation: every refresh revokes the presented token and issues its
  successor in the same family (one family = one sign-in).
* Reuse detection: presenting a token that was already rotated means
  someone holds a copy — the whole family is revoked, so both the thief and
  the user are signed out, and the event is audited. Exception: within
  REFRESH_TOKEN_REUSE_GRACE_SECONDS of the rotation (two tabs refreshing at
  the same moment) the late request just gets an access token, no new
  refresh token (the browser already holds the successor).
* Lifetimes: a token expires after REFRESH_TOKEN_IDLE_DAYS without use, and
  its family after REFRESH_TOKEN_ABSOLUTE_DAYS no matter what.
* `revoke_all_sessions` (password change or reset, sign-out everywhere,
  deactivation, role/company change) revokes every refresh token of the
  account AND bumps users.token_version, which every access token carries —
  so the account's access tokens stop working on the next request too.

Everything here runs on the PLATFORM session (sign-in happens before we know
the company); the caller commits.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.base import utcnow
from app.models.company import Company
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.services.audit_service import record_event

logger = logging.getLogger("fddt.auth")


class RefreshError(Exception):
    """The presented refresh token can't be used; the caller answers 401 and
    clears the cookie. `reason` is for logs only, never shown."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class RefreshedSession:
    user: User
    # The new refresh token to set as the cookie; None inside the reuse grace
    # window (the browser already has it).
    refresh_token: str | None
    family_id: uuid.UUID


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(dt: datetime) -> datetime:
    # SQLite (tests) hands datetimes back naive; they were written in UTC.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _client(request: Request | None) -> tuple[str | None, str | None]:
    if request is None:
        return None, None
    agent = request.headers.get("user-agent")
    ip = request.client.host if request.client else None
    return (agent[:255] if agent else None), (ip[:64] if ip else None)


def account_usable(db: Session, user: User | None) -> bool:
    """Active account in an active company (platform admins have none)."""
    if user is None or not user.is_active:
        return False
    if user.company_id is None:
        return True
    return bool(db.execute(select(Company.is_active).where(Company.id == user.company_id)).scalar_one_or_none())


def issue(
    db: Session,
    user: User,
    request: Request | None,
    *,
    family_id: uuid.UUID | None = None,
    family_expires_at: datetime | None = None,
) -> tuple[str, RefreshToken]:
    """A new refresh token (plaintext, row). Without `family_id` it starts a
    new sign-in."""
    now = utcnow()
    token = secrets.token_urlsafe(32)
    agent, ip = _client(request)
    family_end = family_expires_at or now + timedelta(days=settings.refresh_token_absolute_days)
    row = RefreshToken(
        user_id=user.id,
        family_id=family_id or uuid.uuid4(),
        token_hash=_hash(token),
        token_version=user.token_version,
        created_at=now,
        expires_at=min(now + timedelta(days=settings.refresh_token_idle_days), _aware(family_end)),
        family_expires_at=family_end,
        user_agent=agent,
        ip_address=ip,
    )
    db.add(row)
    db.flush()
    return token, row


def _revoke(db: Session, where, reason: str) -> int:
    return db.execute(
        update(RefreshToken)
        .where(where, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
        .execution_options(synchronize_session=False)
    ).rowcount


def revoke_family(db: Session, family_id: uuid.UUID, reason: str) -> None:
    _revoke(db, RefreshToken.family_id == family_id, reason)


def revoke_all_sessions(db: Session, user: User, reason: str) -> None:
    """Sign the account out everywhere: every refresh token revoked, every
    access token invalidated (token_version bump)."""
    _revoke(db, RefreshToken.user_id == user.id, reason)
    user.token_version = (user.token_version or 0) + 1


def rotate(db: Session, token: str, request: Request | None) -> RefreshedSession:
    """Exchange a refresh token for its successor. Raises RefreshError."""
    if not token or len(token) > 256:
        raise RefreshError("missing")
    row = db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == _hash(token)).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise RefreshError("unknown")
    now = utcnow()
    user = db.get(User, row.user_id)

    if row.revoked_at is not None:
        rotated_recently = (
            row.revoked_reason == "rotated"
            and now - _aware(row.revoked_at) <= timedelta(seconds=settings.refresh_token_reuse_grace_seconds)
        )
        successor = db.get(RefreshToken, row.replaced_by_id) if row.replaced_by_id else None
        if rotated_recently and successor is not None and successor.revoked_at is None and account_usable(db, user):
            if user.token_version == successor.token_version:
                return RefreshedSession(user=user, refresh_token=None, family_id=row.family_id)
        if row.revoked_reason == "rotated":
            # A rotated token came back: it was copied. End that sign-in.
            revoke_family(db, row.family_id, "reuse_detected")
            agent, ip = _client(request)
            logger.warning("refresh_token_reuse user=%s family=%s ip=%s", row.user_id, row.family_id, ip)
            record_event(
                db,
                "refresh_token_reuse_detected",
                actor_user_id=row.user_id,
                company_id=user.company_id if user else None,
                event_data={
                    "user_id": str(row.user_id),
                    "email": user.email if user else None,
                    "ip_address": ip,
                    "user_agent": agent,
                    "summary": "A signed-out session token was used again; that sign-in was ended everywhere.",
                },
            )
            db.commit()
        raise RefreshError(f"revoked:{row.revoked_reason}")

    if _aware(row.expires_at) <= now or _aware(row.family_expires_at) <= now:
        raise RefreshError("expired")
    if not account_usable(db, user) or user.token_version != row.token_version:
        revoke_family(db, row.family_id, "account_changed")
        db.commit()
        raise RefreshError("account")

    new_token, new_row = issue(
        db, user, request, family_id=row.family_id, family_expires_at=_aware(row.family_expires_at)
    )
    row.revoked_at = now
    row.revoked_reason = "rotated"
    row.replaced_by_id = new_row.id
    return RefreshedSession(user=user, refresh_token=new_token, family_id=row.family_id)


def family_of(db: Session, token: str) -> uuid.UUID | None:
    if not token or len(token) > 256:
        return None
    return db.execute(
        select(RefreshToken.family_id).where(RefreshToken.token_hash == _hash(token))
    ).scalar_one_or_none()


def purge_expired(db: Session, *, keep_days: int = 30) -> int:
    """Housekeeping: drop rows whose sign-in ended more than `keep_days` ago
    (expired or revoked). Recent ones are kept for reuse detection and
    investigation."""
    cutoff = utcnow() - timedelta(days=keep_days)
    return db.execute(
        delete(RefreshToken)
        .where(or_(RefreshToken.family_expires_at < cutoff, RefreshToken.revoked_at < cutoff))
        .execution_options(synchronize_session=False)
    ).rowcount
