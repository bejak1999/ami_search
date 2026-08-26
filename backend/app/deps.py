"""Shared FastAPI dependencies: auth, current user, admin gate."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Cookie, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import AuthSession, CostProfile, User, UserRole
from .security import decode_token
from .services import landed_cost

COOKIE_NAME = "amisearch_token"

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def _extract_token(
    authorization: str | None = Header(default=None),
    cookie_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return cookie_token


def current_user(
    token: str | None = Depends(_extract_token),
    db: Session = Depends(get_db),
) -> User:
    if not token:
        raise _UNAUTHORIZED

    payload = decode_token(token)
    if not payload:
        raise _UNAUTHORIZED

    session = db.execute(
        select(AuthSession).where(AuthSession.token_id == payload.get("jti", ""))
    ).scalar_one_or_none()
    if session is None or session.revoked:
        raise _UNAUTHORIZED

    expires = session.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise _UNAUTHORIZED

    user = db.get(User, int(payload["sub"]))
    if user is None or not user.is_active:
        raise _UNAUTHORIZED
    return user


def optional_user(
    token: str | None = Depends(_extract_token),
    db: Session = Depends(get_db),
) -> User | None:
    if not token:
        return None
    try:
        return current_user(token=token, db=db)
    except HTTPException:
        return None


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator access required"
        )
    return user


def user_cost_profile(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> CostProfile:
    if user.cost_profile is None:
        profile = landed_cost.default_profile(user.id)
        db.add(profile)
        db.commit()
        db.refresh(user)
    return user.cost_profile


def client_fingerprint(request: Request) -> tuple[str, str]:
    forwarded = request.headers.get("x-forwarded-for", "")
    ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else ""
    )
    return ip[:64], request.headers.get("user-agent", "")[:255]
