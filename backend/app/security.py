"""Password hashing and JWT session tokens.

bcrypt is used directly rather than through passlib: passlib 1.7.x trips over
bcrypt 4.x internals and only emits noise. Passwords longer than bcrypt's
72-byte input limit are pre-hashed with SHA-256 so long passphrases keep their
full entropy instead of being silently truncated.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from .config import settings

ALGORITHM = "HS256"
_BCRYPT_MAX_BYTES = 72


def _prepare(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) <= _BCRYPT_MAX_BYTES:
        return raw
    return base64.b64encode(hashlib.sha256(raw).digest())


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_prepare(password), hashed.encode("utf-8"))
    except ValueError:
        return False


def new_token_id() -> str:
    return secrets.token_urlsafe(24)


def create_access_token(
    user_id: int, token_id: str, ttl_minutes: int | None = None
) -> tuple[str, datetime]:
    expires_at = datetime.now(timezone.utc) + timedelta(
        minutes=ttl_minutes or settings.access_token_ttl_minutes
    )
    payload = {
        "sub": str(user_id),
        "jti": token_id,
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)
    return token, expires_at


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


def password_strength_problem(password: str) -> str | None:
    """Return a human-readable complaint, or None when the password is fine."""
    if len(password) < 10:
        return "Password must be at least 10 characters long"
    if password.lower() in {"password12", "1234567890", "qwertyuiop"}:
        return "That password is too common"
    classes = sum(
        (
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        )
    )
    if classes < 2:
        return "Use at least two of: lowercase, uppercase, digits, symbols"
    return None
