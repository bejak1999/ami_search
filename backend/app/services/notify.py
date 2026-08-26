"""Turning a trigger into an alert and getting it onto the user's phone."""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..events import bus
from ..models import (
    Alert,
    AlertDelivery,
    ChannelType,
    DeliveryStatus,
    NotificationChannel,
    TriggerType,
    User,
    Watch,
    utcnow,
)
from ..notifiers import Notification, NotifierError, SubscriptionGone, send

log = logging.getLogger(__name__)

#: Triggers that are worth waking someone up for.
URGENT_TRIGGERS = {
    TriggerType.price_below,
    TriggerType.back_in_stock,
    TriggerType.restock_preowned,
}

TRIGGER_LABELS = {
    TriggerType.price_below: "Target price reached",
    TriggerType.price_drop: "Price dropped",
    TriggerType.back_in_stock: "Back in stock",
    TriggerType.restock_preowned: "Pre-owned listing available",
    TriggerType.new_match: "New match",
    TriggerType.deal_radar: "Unusually cheap",
}

TRIGGER_EMOJI = {
    TriggerType.price_below: "\U0001f3af",
    TriggerType.price_drop: "\U0001f4c9",
    TriggerType.back_in_stock: "\U0001f4e6",
    TriggerType.restock_preowned: "\u267b\ufe0f",
    TriggerType.new_match: "\u2728",
    TriggerType.deal_radar: "\U0001f525",
}


def _user_tz(user: User) -> ZoneInfo:
    try:
        return ZoneInfo(user.timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def in_quiet_hours(watch: Watch, user: User, now: datetime | None = None) -> bool:
    """True when the watch is inside its configured quiet window."""
    config = watch.quiet_hours or {}
    if not config.get("enabled"):
        return False
    try:
        start = time.fromisoformat(str(config.get("start", "23:00")))
        end = time.fromisoformat(str(config.get("end", "07:00")))
    except ValueError:
        return False

    local = (now or datetime.now(timezone.utc)).astimezone(_user_tz(user)).time()
    if start <= end:
        return start <= local < end
    # Window wraps past midnight.
    return local >= start or local < end


def format_money(amount: float | None, currency: str) -> str | None:
    if amount is None:
        return None
    if currency.upper() == "JPY":
        return f"{amount:,.0f} JPY"
    return f"{amount:,.2f} {currency.upper()}"


def build_notification(alert: Alert, shop_name: str | None = None) -> Notification:
    trigger = alert.trigger
    emoji = TRIGGER_EMOJI.get(trigger, "\U0001f514")
    fields: dict[str, str] = {}

    if alert.previous_price and alert.price and alert.previous_price > alert.price:
        drop = 1.0 - (alert.price / alert.previous_price)
        fields["Was"] = format_money(alert.previous_price, alert.currency) or ""
        fields["Drop"] = f"-{drop * 100:.0f}%"

    for key in ("condition", "grade", "release", "stock"):
        value = (alert.extra or {}).get(key)
        if value:
            fields[key.capitalize()] = str(value)

    return Notification(
        title=f"{emoji} {alert.title}",
        body=alert.body or "",
        url=alert.url,
        image_url=alert.image_url,
        price_text=format_money(alert.price, alert.currency),
        landed_text=format_money(alert.landed_price, alert.landed_currency),
        trigger=trigger.value,
        item_name=(alert.extra or {}).get("item_name") or alert.title,
        shop=shop_name or (alert.extra or {}).get("shop"),
        urgent=trigger in URGENT_TRIGGERS,
        fields=fields,
    )


def channels_for(db: Session, watch: Watch | None, user: User) -> list[NotificationChannel]:
    stmt = select(NotificationChannel).where(
        NotificationChannel.user_id == user.id,
        NotificationChannel.enabled.is_(True),
    )
    channels = list(db.execute(stmt).scalars().all())
    if watch and watch.channel_ids:
        wanted = {int(c) for c in watch.channel_ids}
        channels = [c for c in channels if c.id in wanted]
    else:
        channels = [c for c in channels if c.is_default]
    return channels


def deliver(
    db: Session,
    alert: Alert,
    user: User,
    watch: Watch | None = None,
    shop_name: str | None = None,
) -> list[AlertDelivery]:
    """Fan the alert out to every selected channel and record the outcome."""
    notification = build_notification(alert, shop_name=shop_name)
    quiet = watch is not None and in_quiet_hours(watch, user)
    urgent_override = bool((watch.quiet_hours or {}).get("urgent_override")) if watch else False
    suppress = quiet and not (notification.urgent and urgent_override)

    deliveries: list[AlertDelivery] = []
    for channel in channels_for(db, watch, user):
        record = AlertDelivery(
            alert_id=alert.id,
            channel_id=channel.id,
            channel_type=channel.type,
            attempts=1,
        )

        digest_only = bool((channel.config or {}).get("digest_only"))
        if suppress or digest_only:
            record.status = DeliveryStatus.skipped
            record.error = "Quiet hours" if suppress else "Channel is digest-only"
            db.add(record)
            deliveries.append(record)
            continue

        try:
            send(channel.type, channel.config or {}, notification)
        except SubscriptionGone as exc:
            # The browser threw the subscription away; disable it so the user
            # sees why push stopped instead of silently losing alerts.
            channel.enabled = False
            channel.last_error = str(exc)
            record.status = DeliveryStatus.failed
            record.error = str(exc)
            log.info("Disabled dead push channel %s", channel.id)
        except NotifierError as exc:
            channel.failure_count += 1
            channel.last_error = str(exc)
            record.status = DeliveryStatus.failed
            record.error = str(exc)[:500]
            log.warning("Channel %s (%s) failed: %s", channel.id, channel.type.value, exc)
        except Exception as exc:  # noqa: BLE001 - never let one channel kill the rest
            channel.failure_count += 1
            channel.last_error = str(exc)
            record.status = DeliveryStatus.failed
            record.error = str(exc)[:500]
            log.exception("Unexpected failure on channel %s", channel.id)
        else:
            channel.failure_count = 0
            channel.last_error = None
            channel.last_used_at = utcnow()
            record.status = DeliveryStatus.sent
            record.sent_at = utcnow()

        db.add(record)
        deliveries.append(record)

    db.commit()

    bus.publish(
        user.id,
        "alert",
        {
            "id": alert.id,
            "trigger": alert.trigger.value,
            "title": alert.title,
            "item_name": (alert.extra or {}).get("item_name"),
            "price": alert.price,
            "currency": alert.currency,
            "landed_price": alert.landed_price,
            "landed_currency": alert.landed_currency,
            "image_url": alert.image_url,
            "url": alert.url,
            "item_id": alert.item_id,
            "watch_id": alert.watch_id,
            "created_at": alert.created_at,
            "suppressed": suppress,
        },
    )
    return deliveries


def send_test(db: Session, channel: NotificationChannel) -> None:
    """Fire a sample alert so the user can prove a channel works."""
    notification = Notification(
        title="\u2705 AmiSearch test notification",
        body="If you can read this, this channel is wired up correctly.",
        item_name="Nendoroid Test Subject 01",
        price_text="6,800 JPY",
        landed_text="52.40 EUR",
        trigger="new_match",
        shop="AmiAmi",
        url="https://www.amiami.com/eng/",
        image_url=None,
        fields={"Condition": "Pre-owned", "Grade": "ITEM:A/BOX:B"},
    )
    try:
        send(channel.type, channel.config or {}, notification)
    except NotifierError:
        channel.failure_count += 1
        db.commit()
        raise
    channel.last_used_at = utcnow()
    channel.last_error = None
    channel.failure_count = 0
    db.commit()
