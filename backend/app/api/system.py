"""Public config, health, status and administration."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import admin_user, current_user
from ..models import (
    Alert,
    Item,
    NotificationChannel,
    PricePoint,
    User,
    UserRole,
    Watch,
)
from ..notifiers import describe_all
from ..providers import all_providers
from ..schemas import MessageResponse, PublicConfig, SystemStatus, UserOut
from ..services import dealradar, fx
from .search import provider_info

router = APIRouter(tags=["system"])

VERSION = "1.0.0"
_STARTED_AT = time.time()


@router.get("/config", response_model=PublicConfig)
def public_config(db: Session = Depends(get_db)) -> PublicConfig:
    """Everything the login screen needs before anyone is authenticated."""
    has_users = bool(db.execute(select(func.count(User.id))).scalar_one())
    return PublicConfig(
        app_name=settings.app_name,
        registration_open=settings.allow_registration or not has_users,
        has_users=has_users,
        vapid_public_key=settings.vapid_public_key or None,
        providers=[provider_info(p) for p in all_providers()],
        channel_types=describe_all(),
        default_currency=settings.display_currency,
        min_poll_interval_seconds=settings.min_poll_interval_seconds,
        version=VERSION,
    )


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "version": VERSION}


@router.get("/status", response_model=SystemStatus)
def system_status(
    db: Session = Depends(get_db), _user: User = Depends(current_user)
) -> SystemStatus:
    from ..scheduler.engine import engine

    def count(model) -> int:
        return int(db.execute(select(func.count(model.id))).scalar_one() or 0)

    return SystemStatus(
        version=VERSION,
        scheduler=engine.status(),
        providers=[provider_info(p) for p in all_providers()],
        fx=fx.snapshot(db),
        database=settings.resolved_database_url.split("://", 1)[0],
        users=count(User),
        watches=count(Watch),
        items=count(Item),
        price_points=count(PricePoint),
        alerts=count(Alert),
        data_dir=str(settings.data_dir),
        webpush_configured=bool(settings.vapid_public_key and settings.vapid_private_key),
        smtp_configured=bool(settings.smtp_host),
        registration_open=settings.allow_registration,
        uptime_seconds=round(time.time() - _STARTED_AT, 1),
    )


@router.post("/fx/refresh", response_model=MessageResponse)
def refresh_fx(db: Session = Depends(get_db), _user: User = Depends(current_user)) -> MessageResponse:
    written = fx.refresh_rates(db)
    if not written:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Every exchange rate source failed"
        )
    return MessageResponse(message=f"Refreshed {written} rate(s)", detail=fx.snapshot(db))


@router.post("/deal-radar/scan", response_model=MessageResponse)
def scan_deals(db: Session = Depends(get_db), user: User = Depends(current_user)) -> MessageResponse:
    found = dealradar.scan(db, user_id=user.id)
    return MessageResponse(message=f"Deal radar raised {found} alert(s)", detail={"alerts": found})


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

admin = APIRouter(prefix="/admin", tags=["admin"])


@admin.get("/users", response_model=list[dict])
def list_users(db: Session = Depends(get_db), _admin: User = Depends(admin_user)) -> list[dict]:
    users = db.execute(select(User).order_by(User.created_at)).scalars()
    out = []
    for user in users:
        out.append(
            {
                **UserOut.model_validate(user).model_dump(mode="json"),
                "watch_count": int(
                    db.execute(
                        select(func.count(Watch.id)).where(Watch.user_id == user.id)
                    ).scalar_one()
                    or 0
                ),
                "channel_count": int(
                    db.execute(
                        select(func.count(NotificationChannel.id)).where(
                            NotificationChannel.user_id == user.id
                        )
                    ).scalar_one()
                    or 0
                ),
                "alert_count": int(
                    db.execute(
                        select(func.count(Alert.id)).where(Alert.user_id == user.id)
                    ).scalar_one()
                    or 0
                ),
            }
        )
    return out


@admin.patch("/users/{user_id}", response_model=MessageResponse)
def update_user(
    user_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    acting_admin: User = Depends(admin_user),
) -> MessageResponse:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if "is_active" in payload:
        if target.id == acting_admin.id and not payload["is_active"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot deactivate your own account",
            )
        target.is_active = bool(payload["is_active"])

    if "role" in payload:
        try:
            new_role = UserRole(payload["role"])
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown role"
            ) from exc
        if target.id == acting_admin.id and new_role != UserRole.admin:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot remove your own administrator role",
            )
        admins_left = int(
            db.execute(
                select(func.count(User.id)).where(
                    User.role == UserRole.admin, User.id != target.id
                )
            ).scalar_one()
            or 0
        )
        if new_role != UserRole.admin and admins_left == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This instance needs at least one administrator",
            )
        target.role = new_role

    db.commit()
    return MessageResponse(message="User updated")


@admin.delete("/users/{user_id}", response_model=MessageResponse)
def delete_user(
    user_id: int, db: Session = Depends(get_db), acting_admin: User = Depends(admin_user)
) -> MessageResponse:
    if user_id == acting_admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own account"
        )
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    db.delete(target)
    db.commit()
    return MessageResponse(message="User and all their data deleted")


@admin.get("/settings", response_model=MessageResponse)
def instance_settings(_admin: User = Depends(admin_user)) -> MessageResponse:
    """Read-only view of the environment configuration, secrets omitted."""
    return MessageResponse(
        message="ok",
        detail={
            "allow_registration": settings.allow_registration,
            "scheduler_enabled": settings.scheduler_enabled,
            "worker_concurrency": settings.worker_concurrency,
            "min_poll_interval_seconds": settings.min_poll_interval_seconds,
            "default_poll_interval_seconds": settings.default_poll_interval_seconds,
            "adaptive_polling": settings.adaptive_polling,
            "provider_requests_per_minute": settings.provider_requests_per_minute,
            "provider_max_concurrency": settings.provider_max_concurrency,
            "fx_refresh_hours": settings.fx_refresh_hours,
            "display_currency": settings.display_currency,
            "price_history_retention_days": settings.price_history_retention_days,
            "alert_retention_days": settings.alert_retention_days,
            "base_url": settings.base_url,
            "smtp_host": settings.smtp_host,
            "smtp_configured": bool(settings.smtp_host),
            "webpush_configured": bool(settings.vapid_public_key),
        },
    )


@admin.post("/vapid/generate", response_model=MessageResponse)
def generate_vapid(_admin: User = Depends(admin_user)) -> MessageResponse:
    """Mint a VAPID keypair. The values still have to be put in the env file."""
    from ..notifiers import generate_vapid_keys

    keys = generate_vapid_keys()
    return MessageResponse(
        message="Add these to your environment, then restart the container",
        detail={
            "VAPID_PUBLIC_KEY": keys["public_key"],
            "VAPID_PRIVATE_KEY": keys["private_key_der"],
        },
    )
