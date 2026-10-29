"""
Password hashing and access-token (JWT) issuing/verification.

Access tokens are short-lived (JWT_ACCESS_TOKEN_EXPIRE_MINUTES, default 15)
and carry `typ=access`, issuer, audience, a unique `jti` and the user's
`ver` (users.token_version): bumping the version — password change or reset,
sign-out everywhere, deactivation — kills every access token already issued.
A session outlives one access token through the rotating refresh token
(app/services/refresh_tokens.py), never through a long-lived JWT.
"""
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.core.config import settings

ACCESS_TOKEN_TYPE = "access"
_REQUIRED_CLAIMS = ["sub", "exp", "iat", "nbf", "iss", "aud", "jti", "typ", "ver"]
_WEAK_SECRETS = {"changeme-in-.env", "changeme", "secret", ""}


def hash_password(plain_password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(plain_password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        return False


# A real bcrypt hash of a random string: checked against when the email is
# unknown, so a sign-in takes the same time whether or not the account exists.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def burn_password_check(plain_password: str) -> None:
    verify_password(plain_password, _DUMMY_HASH)


def create_access_token(subject: str, extra_claims: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    to_encode: dict[str, Any] = {
        "ver": 0,
        **(extra_claims or {}),
        "sub": subject,
        "iat": now,
        "nbf": now,
        "exp": expire,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "jti": uuid.uuid4().hex,
        "typ": ACCESS_TOKEN_TYPE,
    }
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """The claims of a valid, unexpired access token, else None. The
    algorithm is pinned (no `alg` from the token is trusted) and every
    claim this API relies on must be present."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=10,
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        return None
    return payload


def access_token_lifetime_seconds() -> int:
    return settings.jwt_access_token_expire_minutes * 60


def check_secret_strength() -> None:
    """Refuse to run outside local/test with a guessable signing key: anyone
    who knows it can mint a token for any account."""
    if settings.is_dev_environment:
        return
    key = settings.jwt_secret_key
    if key in _WEAK_SECRETS or len(key) < 32:
        raise RuntimeError(
            "JWT_SECRET_KEY is missing or too weak for ENVIRONMENT="
            f"{settings.environment!r}: set a random value of at least 32 characters "
            "(e.g. `python -c \"import secrets; print(secrets.token_urlsafe(48))\"`)."
        )
