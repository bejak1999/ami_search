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
    Condition,
    Item,
    NotificationChannel,
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

#: How far a wishlisted figure has to fall against its own previous price
#: before it is worth interrupting somebody for.
DEFAULT_DROP = 0.25


def _settings_for(user: User) -> dict:
    prefs = (user.prefs or {}).get("deal_radar") or {}
    return {
        "enabled": prefs.get("enabled", True),
        "discount": float(prefs.get("discount", DEFAULT_DISCOUNT)),
        "include_watched": bool(prefs.get("include_watched", True)),
        "in_stock_only": bool(prefs.get("in_stock_only", True)),
    }


def lowest_threshold(db: Session, user: User, trigger: TriggerType, fallback: float) -> float:
    """The most generous bar any enabled channel has set for this alert.

    Channels may each set their own - the phone from a quarter off, the
    mailbox only for something drastic - and the alert has to exist before
    any of them can be offered it. So the scan runs at whichever bar is
    lowest, and delivery is what turns the stricter channels away.

    Scanning at the user's own figure alone would mean a channel asking for
    ten per cent never hearing about a fifteen per cent drop, because nothing
    was ever raised for it to receive.
    """
    bars = [fallback]
    channels = db.execute(
        select(NotificationChannel).where(
            NotificationChannel.user_id == user.id,
            NotificationChannel.enabled.is_(True),
        )
    ).scalars()
    for channel in channels:
        thresholds = (channel.config or {}).get("thresholds")
        if not isinstance(thresholds, dict):
            continue
        bar = thresholds.get(trigger.value)
        if bar is None:
            continue
        try:
            bars.append(float(bar))
        except (TypeError, ValueError):  # pragma: no cover - a hand-edited row
            continue
    return max(0.01, min(bars))


def _drop_settings(user: User) -> dict:
    """Whether to report a wishlisted figure falling sharply, and how sharply.

    Off unless asked for. This one interrupts you about a figure you already
    said you want, which is exactly the alert nobody wants arriving by
    surprise on the day they installed the application.
    """
    prefs = (user.prefs or {}).get("price_drop") or {}
    return {
        "enabled": bool(prefs.get("enabled", False)),
        "percent": max(0.01, min(0.95, float(prefs.get("percent", DEFAULT_DROP)))),
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


def _recently_alerted(
    db: Session,
    user_id: int,
    item_id: int,
    trigger: TriggerType = TriggerType.deal_radar,
) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=RECHECK_HOURS)
    return (
        db.execute(
            select(Alert.id).where(
                Alert.user_id == user_id,
                Alert.item_id == item_id,
                Alert.trigger == trigger,
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
        # Two independent questions, two switches. "Cheap against its own
        # history" and "cheaper than it was this morning" are different
        # things, and having either on must not require the other.
        raised += _scan_price_drops(db, user)

        config = _settings_for(user)
        if not config["enabled"]:
            continue

        radar_bar = lowest_threshold(
            db, user, TriggerType.deal_radar, config["discount"]
        )
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
            if discount < radar_bar:
                continue

            raised += _raise_alert(db, user, item, baseline, discount, samples)

    return raised


def _scan_price_drops(db: Session, user: User) -> int:
    """Report a wishlisted figure that has fallen sharply since the last look.

    Measured against what the figure last cost, not against its tracked
    history: a used copy undercutting a sold-out new listing has no history
    of its own to be cheap against, and that is precisely the case worth
    hearing about. The comparison spans both listings, because someone who
    saved the new one is waiting for whichever of them becomes buyable.

    Only a change reports. The reference moves every scan, so a figure that
    has sat with a wide grade spread for weeks says nothing - there is no
    news in a gap that has always been there - while a copy arriving below
    what the figure cost this morning does.
    """
    config = _drop_settings(user)
    if not config["enabled"]:
        return 0
    threshold = lowest_threshold(
        db, user, TriggerType.price_drop, config["percent"]
    )

    entries = list(
        db.execute(
            select(CollectionEntry).where(
                CollectionEntry.user_id == user.id,
                CollectionEntry.status == CollectionStatus.wishlist,
            )
        )
        .scalars()
        .all()
    )
    raised = 0
    for entry in entries:
        item = entry.item
        if item is None:
            continue
        price, where = catalog.cheapest_buyable(db, item)
        if price is None or where is None:
            # Nothing to buy. The reference is left where it is, so a copy
            # coming back at the price it always had is not read as a drop.
            #
            # Unless there is no reference at all. Most of a wishlist is
            # figures saved from a new listing that was already sold out -
            # there is no used one to save until somebody has sold one back -
            # and such an entry would never acquire a reference at all. The
            # used copy that eventually appears, which is the whole reason
            # for saving the figure, was then swallowed as "first look".
            #
            # What it last cost is the honest starting point. That price may
            # be months old, and it is still the only thing there is.
            if not entry.drop_reference_price:
                fallback = catalog.last_known_price(db, item)
                if fallback:
                    entry.drop_reference_price = fallback
                    entry.drop_reference_at = utcnow()
            continue

        reference = entry.drop_reference_price
        entry.drop_reference_at = utcnow()
        entry.drop_reference_price = price
        if not reference or reference <= 0:
            # First look at this figure. It sets the fixed point and reports
            # nothing, because "cheaper than the first price we ever saw" is
            # not a fact about the shop.
            continue

        drop = 1.0 - (price / reference)
        if drop < threshold:
            continue
        if _recently_alerted(db, user.id, where.id, TriggerType.price_drop):
            continue
        raised += _raise_drop_alert(db, user, where, reference, price, drop)

    db.commit()
    return raised


def _raise_drop_alert(
    db: Session, user: User, item: Item, reference: float, price: float, drop: float
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
    used = item.condition == Condition.preowned
    alert = Alert(
        user_id=user.id,
        item_id=item.id,
        trigger=TriggerType.price_drop,
        title=f"{drop * 100:.0f}% cheaper than before: {item.name[:80]}",
        body=(
            "On your wishlist. Last buyable at "
            f"{notify.format_money(reference, item.currency)}, "
            f"now {notify.format_money(price, item.currency)}"
            + (" as a used copy." if used else " as a new listing.")
        ),
        price=price,
        currency=item.currency,
        landed_price=valuation.landed_total,
        landed_currency=valuation.landed_currency,
        previous_price=reference,
        url=item.product_url,
        image_url=item.image_url,
        extra={
            "item_name": item.name,
            "shop": item.provider,
            "previous": reference,
            "drop_pct": round(drop * 100, 1),
            "condition": item.condition.value,
        },
    )
    db.add(alert)
    db.commit()
    notify.deliver(db, alert, user, watch=None, shop_name=item.provider)
    return 1


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
