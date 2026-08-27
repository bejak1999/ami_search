"""Dashboard aggregates."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, user_cost_profile
from ..models import (
    Alert,
    CollectionEntry,
    CollectionStatus,
    CostProfile,
    Item,
    TriggerType,
    User,
    Watch,
    WatchSeenItem,
)
from ..schemas import DashboardStats
from ..services import fx
from .serializers import alert_out, item_out, register_images

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardStats)
def dashboard(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> DashboardStats:
    now = datetime.now(timezone.utc)
    display = (user.display_currency or "EUR").upper()

    def count(stmt) -> int:
        return int(db.execute(stmt).scalar_one() or 0)

    watches_total = count(select(func.count(Watch.id)).where(Watch.user_id == user.id))
    watches_active = count(
        select(func.count(Watch.id)).where(Watch.user_id == user.id, Watch.enabled.is_(True))
    )
    alerts_24h = count(
        select(func.count(Alert.id)).where(
            Alert.user_id == user.id, Alert.created_at >= now - timedelta(days=1)
        )
    )
    alerts_7d = count(
        select(func.count(Alert.id)).where(
            Alert.user_id == user.id, Alert.created_at >= now - timedelta(days=7)
        )
    )
    alerts_unread = count(
        select(func.count(Alert.id)).where(Alert.user_id == user.id, Alert.read_at.is_(None))
    )
    price_drops_7d = count(
        select(func.count(Alert.id)).where(
            Alert.user_id == user.id,
            Alert.created_at >= now - timedelta(days=7),
            Alert.trigger.in_([TriggerType.price_drop, TriggerType.price_below]),
        )
    )
    items_tracked = count(
        select(func.count(func.distinct(WatchSeenItem.item_id)))
        .join(Watch, Watch.id == WatchSeenItem.watch_id)
        .where(Watch.user_id == user.id)
    )
    wishlist_count = count(
        select(func.count(CollectionEntry.id)).where(
            CollectionEntry.user_id == user.id,
            CollectionEntry.status == CollectionStatus.wishlist,
        )
    )

    next_check_at = db.execute(
        select(func.min(Watch.next_run_at)).where(
            Watch.user_id == user.id, Watch.enabled.is_(True)
        )
    ).scalar_one_or_none()

    owned_value = 0.0
    for entry in db.execute(
        select(CollectionEntry).where(
            CollectionEntry.user_id == user.id,
            CollectionEntry.status == CollectionStatus.owned,
        )
    ).scalars():
        converted = fx.convert(db, entry.item.current_price, entry.item.currency, display)
        owned_value += (converted or 0.0) * entry.quantity

    cheapest_rows = db.execute(
        select(Item)
        .join(CollectionEntry, CollectionEntry.item_id == Item.id)
        .where(
            CollectionEntry.user_id == user.id,
            CollectionEntry.status == CollectionStatus.wishlist,
            Item.in_stock.is_(True),
        )
        .order_by(Item.current_price.asc().nulls_last())
        .limit(6)
    ).scalars()
    cheapest = list(cheapest_rows)
    register_images(db, cheapest)

    recent = db.execute(
        select(Alert)
        .where(Alert.user_id == user.id)
        .order_by(Alert.created_at.desc())
        .limit(8)
    ).scalars()

    return DashboardStats(
        watches_active=watches_active,
        watches_total=watches_total,
        alerts_24h=alerts_24h,
        alerts_7d=alerts_7d,
        alerts_unread=alerts_unread,
        items_tracked=items_tracked,
        wishlist_count=wishlist_count,
        collection_value=round(owned_value, 2) if owned_value else None,
        collection_currency=display,
        next_check_at=next_check_at,
        cheapest_wishlist=[
            item_out(db, item, user=user, profile=profile, with_context=True) for item in cheapest
        ],
        recent_alerts=[alert_out(db, a) for a in recent],
        price_drops_7d=price_drops_7d,
    )
