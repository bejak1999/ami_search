"""Registration, login, session management and profile settings."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import COOKIE_NAME, client_fingerprint, current_user
from ..models import AuthSession, CostProfile, Item, User, UserRole, utcnow
from ..schemas import (
    AuthResponse,
    ChangePasswordRequest,
    CostProfileOut,
    CostProfilePreview,
    CostProfileUpdate,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    UserOut,
    UserUpdate,
)
from ..security import (
    create_access_token,
    hash_password,
    new_token_id,
    password_strength_problem,
    verify_password,
)
from ..services import landed_cost, shipping_rates

router = APIRouter(prefix="/auth", tags=["auth"])

#: A typical order, used only to show the user what their settings do.
SAMPLE_PRICE_JPY = 10000.0
SAMPLE_ITEM = Item(name="1/7 Scale Figure", scale="1/7", category="Figure")


def _user_count(db: Session) -> int:
    return int(db.execute(select(func.count(User.id))).scalar_one() or 0)


def _issue(db: Session, user: User, request: Request, response: Response, remember: bool) -> AuthResponse:
    ip, agent = client_fingerprint(request)
    token_id = new_token_id()
    ttl = settings.access_token_ttl_minutes if remember else 12 * 60
    token, expires_at = create_access_token(user.id, token_id, ttl_minutes=ttl)

    db.add(
        AuthSession(
            user_id=user.id,
            token_id=token_id,
            user_agent=agent,
            ip=ip,
            expires_at=expires_at,
        )
    )
    user.last_login_at = utcnow()
    db.commit()

    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int((expires_at - datetime.now(timezone.utc)).total_seconds()),
        httponly=True,
        samesite="lax",
        secure=settings.base_url.startswith("https"),
        path="/",
    )
    return AuthResponse(user=UserOut.model_validate(user), token=token, expires_at=expires_at)


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> AuthResponse:
    existing_users = _user_count(db)
    # The very first account is always allowed, otherwise a fresh instance
    # with registration disabled could never be set up.
    if existing_users and not settings.allow_registration:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is disabled on this instance",
        )

    problem = password_strength_problem(payload.password)
    if problem:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=problem)

    clash = db.execute(
        select(User).where(
            or_(User.email == payload.email.lower(), User.username == payload.username)
        )
    ).scalar_one_or_none()
    if clash is not None:
        field = "e-mail address" if clash.email == payload.email.lower() else "username"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"That {field} is already taken"
        )

    user = User(
        email=payload.email.lower(),
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=UserRole.admin if existing_users == 0 else UserRole.user,
        display_currency=settings.display_currency,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    db.add(landed_cost.default_profile(user.id))
    db.commit()

    return _issue(db, user, request, response, remember=True)


@router.post("/login", response_model=AuthResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> AuthResponse:
    identifier = payload.identifier.strip().lower()
    user = db.execute(
        select(User).where(or_(User.email == identifier, User.username == payload.identifier.strip()))
    ).scalar_one_or_none()

    # Always run a hash comparison so a missing account and a wrong password
    # take the same amount of time.
    hashed = user.password_hash if user else "$2b$12$" + "." * 53
    if not verify_password(payload.password, hashed) or user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Wrong username or password"
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="This account has been disabled"
        )
    return _issue(db, user, request, response, remember=payload.remember)


@router.post("/logout", response_model=MessageResponse)
def logout(
    response: Response,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> MessageResponse:
    from ..security import decode_token

    token = request.cookies.get(COOKIE_NAME) or ""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        token = header[7:].strip()

    payload = decode_token(token) or {}
    session = db.execute(
        select(AuthSession).where(AuthSession.token_id == payload.get("jti", ""))
    ).scalar_one_or_none()
    if session is not None:
        session.revoked = True
        db.commit()

    response.delete_cookie(COOKIE_NAME, path="/")
    return MessageResponse(message="Signed out")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut.model_validate(user)


@router.patch("/me", response_model=UserOut)
def update_me(
    payload: UserUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> UserOut:
    data = payload.model_dump(exclude_unset=True)
    if "display_currency" in data and data["display_currency"]:
        data["display_currency"] = data["display_currency"].upper()
    if "prefs" in data and data["prefs"] is not None:
        # Merge rather than replace so one settings panel cannot clobber another.
        merged = dict(user.prefs or {})
        merged.update(data.pop("prefs"))
        user.prefs = merged
    for key, value in data.items():
        setattr(user, key, value)
    db.commit()
    db.refresh(user)
    return UserOut.model_validate(user)


@router.post("/password", response_model=MessageResponse)
def change_password(
    payload: ChangePasswordRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> MessageResponse:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is wrong"
        )
    problem = password_strength_problem(payload.new_password)
    if problem:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=problem)

    user.password_hash = hash_password(payload.new_password)
    # Changing a password invalidates every other session.
    for session in db.execute(
        select(AuthSession).where(AuthSession.user_id == user.id)
    ).scalars():
        session.revoked = True
    db.commit()
    return MessageResponse(message="Password changed. Please sign in again.")


@router.get("/sessions")
def list_sessions(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[dict]:
    rows = db.execute(
        select(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.revoked.is_(False))
        .order_by(AuthSession.created_at.desc())
    ).scalars()
    return [
        {
            "id": s.id,
            "user_agent": s.user_agent,
            "ip": s.ip,
            "created_at": s.created_at,
            "expires_at": s.expires_at,
        }
        for s in rows
    ]


@router.delete("/sessions/{session_id}", response_model=MessageResponse)
def revoke_session(
    session_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> MessageResponse:
    session = db.get(AuthSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    session.revoked = True
    db.commit()
    return MessageResponse(message="Session revoked")


@router.get("/cost-profile", response_model=CostProfileOut)
def get_cost_profile(
    db: Session = Depends(get_db), user: User = Depends(current_user)
) -> CostProfileOut:
    if user.cost_profile is None:
        db.add(landed_cost.default_profile(user.id))
        db.commit()
        db.refresh(user)
    return CostProfileOut.model_validate(user.cost_profile)


@router.get("/shipping-zones")
def shipping_zones() -> dict:
    """AmiAmi's zones and services, so the settings page can label them."""
    return {
        "zones": [{"value": k, "label": v} for k, v in shipping_rates.ZONES.items()],
        "services": [{"value": k, "label": v} for k, v in shipping_rates.SERVICES.items()],
    }


@router.post("/cost-profile/preview", response_model=CostProfilePreview)
def preview_cost_profile(
    payload: CostProfileUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> CostProfilePreview:
    """Price a sample figure against unsaved settings.

    The settings page used to approximate this in the browser with a guessed
    exchange rate, which stopped being honest the moment shipping came off a
    real rate chart. This runs the actual estimator instead, on a detached
    copy of the profile so nothing touches the database.
    """
    base = user.cost_profile or landed_cost.default_profile(user.id)
    profile = CostProfile(
        user_id=user.id,
        **{
            column.key: getattr(base, column.key)
            for column in CostProfile.__mapper__.column_attrs
            if column.key not in {"id", "user_id"}
        },
    )
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key == "shipping_table" and value is not None:
            value = [{"max_grams": b["max_grams"], "cost": b["cost"]} for b in value]
        if key == "country" and value:
            value = value.upper()
        setattr(profile, key, value)

    sample = SAMPLE_ITEM
    currency = user.display_currency or "EUR"
    breakdown = landed_cost.estimate(
        db, SAMPLE_PRICE_JPY, "JPY", profile, target_currency=currency, item=sample
    )
    return CostProfilePreview(
        sample_price=SAMPLE_PRICE_JPY,
        sample_currency=currency,
        weight_grams=landed_cost.shipment_weight(sample, profile),
        packaging_grams=max(0, int(profile.packaging_grams or 0)),
        breakdown=breakdown.as_dict() if breakdown else None,
    )


@router.patch("/cost-profile", response_model=CostProfileOut)
def update_cost_profile(
    payload: CostProfileUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> CostProfileOut:
    if user.cost_profile is None:
        db.add(landed_cost.default_profile(user.id))
        db.commit()
        db.refresh(user)

    profile = user.cost_profile
    data = payload.model_dump(exclude_unset=True)
    if data.get("shipping_table") is not None:
        data["shipping_table"] = [
            {"max_grams": b["max_grams"], "cost": b["cost"]} for b in data["shipping_table"]
        ]
    if data.get("country"):
        data["country"] = data["country"].upper()
    for key, value in data.items():
        setattr(profile, key, value)
    db.commit()
    db.refresh(profile)
    return CostProfileOut.model_validate(profile)
