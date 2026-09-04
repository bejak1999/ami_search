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


#: Alerts that carry a percentage, and where each keeps it. A channel can set
#: its own bar for these - the phone from a quarter off, the mailbox only for
#: something drastic - so the figure has to be readable back off the alert.
PERCENTAGE_ALERTS: dict[str, str] = {
    TriggerType.price_drop.value: "drop_pct",
    TriggerType.deal_radar.value: "discount_pct",
}


def alert_percent(alert: Alert) -> float | None:
    """How far this alert says the price fell, where it says anything."""
    key = PERCENTAGE_ALERTS.get(
        alert.trigger.value if hasattr(alert.trigger, "value") else str(alert.trigger)
    )
    if not key:
        return None
    value = (alert.extra or {}).get(key)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):  # pragma: no cover - a hand-edited row
        return None


def channel_declines(channel: NotificationChannel, alert: Alert) -> str | None:
    """Why this channel does not want this alert, or None if it does.

    Two independent filters, both optional and both absent by default, so a
    channel configured before either existed keeps receiving everything.

    ``triggers`` names the kinds this channel accepts. ``thresholds`` sets its
    own bar for the ones that carry a percentage, which is the point of having
    it per channel: the same figure falling twenty per cent is worth a push
    notification and not worth an e-mail.
    """
    config = channel.config or {}

    trigger = alert.trigger.value if hasattr(alert.trigger, "value") else str(alert.trigger)
    wanted = config.get("triggers")
    if isinstance(wanted, list) and wanted and trigger not in wanted:
        return "This channel does not take that kind of alert"

    thresholds = config.get("thresholds")
    if isinstance(thresholds, dict):
        bar = thresholds.get(trigger)
        percent = alert_percent(alert)
        if bar is not None and percent is not None:
            try:
                needed = float(bar) * 100.0
            except (TypeError, ValueError):  # pragma: no cover
                return None
            if percent < needed:
                return f"Below this channel's {needed:.0f}% threshold"
    return None


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
        declined = channel_declines(channel, alert)
        if suppress or digest_only or declined:
            record.status = DeliveryStatus.skipped
            record.error = (
                "Quiet hours"
                if suppress
                else ("Channel is digest-only" if digest_only else declined)
            )
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
