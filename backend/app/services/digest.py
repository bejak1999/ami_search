"""Periodic digests and alert retention.

Push channels handle the urgent case. The digest is for the slow half of the
hobby: what dropped this week, what is still sitting above your target, what
you might want to reconsider.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Alert, CollectionEntry, CollectionStatus, Item, NotificationChannel, User
from ..notifiers import Notification, NotifierError, send
from . import notify

log = logging.getLogger(__name__)

FREQUENCIES = {"off": None, "daily": timedelta(days=1), "weekly": timedelta(days=7)}


def _config(user: User) -> dict:
    prefs = (user.prefs or {}).get("digest") or {}
    return {
        "frequency": prefs.get("frequency", "daily"),
        "hour": int(prefs.get("hour", 9)),
        "last_sent": prefs.get("last_sent"),
        "include_wishlist": bool(prefs.get("include_wishlist", True)),
    }


def _mark_sent(db: Session, user: User) -> None:
    prefs = dict(user.prefs or {})
    digest_prefs = dict(prefs.get("digest") or {})
    digest_prefs["last_sent"] = datetime.now(timezone.utc).isoformat()
    prefs["digest"] = digest_prefs
    user.prefs = prefs
    db.commit()


def is_due(user: User, now: datetime | None = None) -> bool:
    config = _config(user)
    interval = FREQUENCIES.get(config["frequency"])
    if interval is None:
        return False

    now = now or datetime.now(timezone.utc)
    last_raw = config.get("last_sent")
    if last_raw:
        try:
            last = datetime.fromisoformat(str(last_raw))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except ValueError:
            last = None
        if last and now - last < interval:
            return False

    # Respect the user's preferred send hour in their own timezone.
    try:
        from zoneinfo import ZoneInfo

        local_hour = now.astimezone(ZoneInfo(user.timezone or "UTC")).hour
    except Exception:  # noqa: BLE001
        local_hour = now.hour
    return local_hour == config["hour"]


def build(db: Session, user: User, since: datetime) -> Notification | None:
    alerts = list(
        db.execute(
            select(Alert)
            .where(Alert.user_id == user.id, Alert.created_at >= since)
            .order_by(Alert.created_at.desc())
            .limit(40)
        )
        .scalars()
        .all()
    )

    wishlist_lines: list[str] = []
    if _config(user)["include_wishlist"]:
        rows = db.execute(
            select(CollectionEntry, Item)
            .join(Item, Item.id == CollectionEntry.item_id)
            .where(
                CollectionEntry.user_id == user.id,
                CollectionEntry.status == CollectionStatus.wishlist,
                Item.in_stock.is_(True),
            )
            .limit(10)
        ).all()
        for _entry, item in rows:
            price = notify.format_money(item.current_price, item.currency)
            wishlist_lines.append(f"- {item.name[:60]} ... {price}")

    if not alerts and not wishlist_lines:
        return None

    body_lines: list[str] = []
    if alerts:
        body_lines.append(f"{len(alerts)} alert(s) since the last digest:")
        for alert in alerts[:15]:
            price = notify.format_money(alert.price, alert.currency) or "price unknown"
            label = notify.TRIGGER_LABELS.get(alert.trigger, alert.trigger.value)
            name = (alert.extra or {}).get("item_name") or alert.title
            body_lines.append(f"- [{label}] {name[:60]} ... {price}")
        if len(alerts) > 15:
            body_lines.append(f"...and {len(alerts) - 15} more")

    if wishlist_lines:
        body_lines.append("")
        body_lines.append("Wishlist items currently in stock:")
        body_lines.extend(wishlist_lines)

    return Notification(
        title=f"AmiSearch digest: {len(alerts)} alert(s)",
        body="\n".join(body_lines),
        trigger="digest",
        item_name=None,
        shop="AmiSearch",
        urgent=False,
    )


def send_digest(db: Session, user: User, force: bool = False) -> bool:
    """Build and send one digest. Returns True when something was sent."""
    config = _config(user)
    interval = FREQUENCIES.get(config["frequency"]) or timedelta(days=1)
    since = datetime.now(timezone.utc) - interval

    notification = build(db, user, since)
    if notification is None:
        if not force:
            return False
        notification = Notification(
            title="AmiSearch digest",
            body="Nothing new since the last digest. Your watches are running.",
            trigger="digest",
            shop="AmiSearch",
        )

    channels = list(
        db.execute(
            select(NotificationChannel).where(
                NotificationChannel.user_id == user.id,
                NotificationChannel.enabled.is_(True),
                NotificationChannel.send_digest.is_(True),
            )
        )
        .scalars()
        .all()
    )
    if not channels:
        return False

    sent = False
    for channel in channels:
        try:
            send(channel.type, channel.config or {}, notification)
            sent = True
        except NotifierError as exc:
            channel.last_error = str(exc)[:500]
            log.warning("Digest to channel %s failed: %s", channel.id, exc)
    if sent:
        _mark_sent(db, user)
    db.commit()
    return sent


def dispatch_due(db: Session) -> int:
    count = 0
    users = db.execute(select(User).where(User.is_active.is_(True))).scalars().all()
    for user in users:
        if is_due(user) and send_digest(db, user):
            count += 1
    return count


def prune_alerts(db: Session, retention_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    result = db.execute(delete(Alert).where(Alert.created_at < cutoff))
    db.commit()
    deleted = int(result.rowcount or 0)
    if deleted:
        log.info("Pruned %s alerts older than %s days", deleted, retention_days)
    return deleted
