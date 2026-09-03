"""Deal radar.

Watches answer "tell me when this hits my price". The radar answers a
different question: "is anything I care about unusually cheap right now?"
It compares the live price against that item's own tracked history, so a
figure that normally sits at 20k JPY and suddenly lists at 11k surfaces even
if no watch was ever configured for it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    Alert,
    CollectionEntry,
    CollectionStatus,
    Item,
    PricePoint,
    TriggerType,
    User,
    Watch,
    WatchSeenItem,
    utcnow,
)
from . import catalog, matcher, notify

log = logging.getLogger(__name__)

DEFAULT_DISCOUNT = 0.25
MIN_HISTORY_POINTS = 4
RECHECK_HOURS = 48


def _settings_for(user: User) -> dict:
    prefs = (user.prefs or {}).get("deal_radar") or {}
    return {
        "enabled": prefs.get("enabled", True),
        "discount": float(prefs.get("discount", DEFAULT_DISCOUNT)),
        "include_watched": bool(prefs.get("include_watched", True)),
        "in_stock_only": bool(prefs.get("in_stock_only", True)),
    }


def _candidate_item_ids(db: Session, user: User, include_watched: bool) -> set[int]:
    ids = set(
        db.execute(
            select(CollectionEntry.item_id).where(
                CollectionEntry.user_id == user.id,
                CollectionEntry.status.in_(
                    [CollectionStatus.wishlist, CollectionStatus.ordered]
                ),
            )
        )
        .scalars()
        .all()
    )
    if include_watched:
        ids |= set(
            db.execute(
                select(WatchSeenItem.item_id)
                .join(Watch, Watch.id == WatchSeenItem.watch_id)
                .where(Watch.user_id == user.id, Watch.enabled.is_(True))
            )
            .scalars()
            .all()
        )
    # Both listings of every figure. Saving the new one because no used copy
    # existed yet - which is how most of a wishlist gets built - otherwise
    # meant the radar never looked at the used copy when it finally appeared,
    # and a used copy is where the bargains are.
    return catalog.with_counterparts(db, ids)


def _baseline(db: Session, item_id: int) -> tuple[float | None, int]:
    """Median-ish reference price from history, plus the sample size.

    The median is used rather than the mean because one flash-sale outlier
    should not drag the baseline down and mute every future alert.
    """
    prices = list(
        db.execute(
            select(PricePoint.price).where(
                PricePoint.item_id == item_id, PricePoint.price.is_not(None)
            )
        )
        .scalars()
        .all()
    )
    if len(prices) < MIN_HISTORY_POINTS:
        return None, len(prices)
    prices.sort()
    middle = len(prices) // 2
    if len(prices) % 2:
        return float(prices[middle]), len(prices)
    return (float(prices[middle - 1]) + float(prices[middle])) / 2.0, len(prices)


def _recently_alerted(db: Session, user_id: int, item_id: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=RECHECK_HOURS)
    return (
        db.execute(
            select(Alert.id).where(
                Alert.user_id == user_id,
                Alert.item_id == item_id,
                Alert.trigger == TriggerType.deal_radar,
                Alert.created_at >= cutoff,
            )
        ).first()
        is not None
    )


def scan(db: Session, user_id: int | None = None) -> int:
    """Run the radar for one user or everyone. Returns alerts raised."""
    users = (
        [db.get(User, user_id)]
        if user_id
        else list(db.execute(select(User).where(User.is_active.is_(True))).scalars().all())
    )
    raised = 0

    for user in filter(None, users):
        config = _settings_for(user)
        if not config["enabled"]:
            continue

        item_ids = _candidate_item_ids(db, user, config["include_watched"])
        if not item_ids:
            continue

        for item in db.execute(select(Item).where(Item.id.in_(item_ids))).scalars():
            if item.current_price is None:
                continue
            if config["in_stock_only"] and not item.in_stock:
                continue
            if _recently_alerted(db, user.id, item.id):
                continue

            baseline, samples = _baseline(db, item.id)
            if not baseline or baseline <= 0:
                continue

            discount = 1.0 - (item.current_price / baseline)
            if discount < config["discount"]:
                continue

            raised += _raise_alert(db, user, item, baseline, discount, samples)

    return raised


def _raise_alert(
    db: Session, user: User, item: Item, baseline: float, discount: float, samples: int
) -> int:
    valuation = matcher.value_item(
        db,
        item,
        Watch(
            user_id=user.id,
            target_currency=user.display_currency or "EUR",
            provider=item.provider,
        ),
        user,
    )
    alert = Alert(
        user_id=user.id,
        item_id=item.id,
        trigger=TriggerType.deal_radar,
        title=f"{discount * 100:.0f}% below its usual price: {item.name[:80]}",
        body=(
            f"Typically around {notify.format_money(baseline, item.currency)} "
            f"across {samples} tracked observations."
        ),
        price=item.current_price,
        currency=item.currency,
        landed_price=valuation.landed_total,
        landed_currency=valuation.landed_currency,
        previous_price=baseline,
        url=item.product_url,
        image_url=item.image_url,
        extra={
            "item_name": item.name,
            "shop": item.provider,
            "baseline": baseline,
            "discount_pct": round(discount * 100, 1),
            "samples": samples,
        },
    )
    db.add(alert)
    db.commit()
    notify.deliver(db, alert, user, watch=None, shop_name=item.provider)
    return 1
