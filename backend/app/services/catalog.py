"""Persisting provider results and maintaining price history."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Condition, Item, PricePoint, utcnow
from ..providers import NormalizedItem

log = logging.getLogger(__name__)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def get_item(db: Session, provider: str, code: str) -> Item | None:
    return db.execute(
        select(Item).where(Item.provider == provider, Item.code == code)
    ).scalar_one_or_none()


def upsert_item(db: Session, normalized: NormalizedItem, commit: bool = True) -> tuple[Item, bool]:
    """Insert or update an item. Returns (item, changed).

    ``changed`` is True when the price or the availability moved, which is the
    signal both the history writer and the alert matcher care about.
    """
    item = get_item(db, normalized.provider, normalized.code)
    created = item is None

    if item is None:
        item = Item(provider=normalized.provider, code=normalized.code)
        db.add(item)

    previous_price = item.current_price
    previous_stock = item.in_stock
    previous_max = item.price_max

    item.name = normalized.name or item.name
    if normalized.name_jp:
        item.name_jp = normalized.name_jp
    if normalized.maker:
        item.maker = normalized.maker
    if normalized.series:
        item.series = normalized.series
    if normalized.character:
        item.character = normalized.character
    if normalized.category:
        item.category = normalized.category
    if normalized.scale:
        item.scale = normalized.scale
    if normalized.jan_code:
        item.jan_code = normalized.jan_code
    if normalized.image_url:
        item.image_url = normalized.image_url
    if normalized.images:
        # Detail responses carry the full gallery; list responses carry one
        # thumbnail, so never let a list poll shrink an existing gallery.
        if len(normalized.images) >= len(item.images or []):
            item.images = normalized.images
    if normalized.spec:
        item.spec = normalized.spec
    if normalized.remarks:
        item.remarks = normalized.remarks

    item.product_url = normalized.url or item.product_url
    item.currency = normalized.currency or item.currency
    item.current_price = normalized.price
    if normalized.price_max is not None:
        item.price_max = normalized.price_max
    if normalized.variants:
        # Only the detail endpoint knows the individual graded listings, so a
        # cheaper list-only poll must not wipe what we already learned.
        item.variants = normalized.variants
    if normalized.list_price:
        item.list_price = normalized.list_price
    item.condition = Condition(normalized.condition)
    if normalized.condition_grade:
        item.condition_grade = normalized.condition_grade
    item.in_stock = normalized.in_stock
    item.is_preorder = normalized.is_preorder
    item.is_backorder = normalized.is_backorder
    item.order_closed = normalized.order_closed
    item.sale_status = normalized.sale_status
    item.release_date = normalized.release_date
    item.release_date_parsed = normalized.release_date_parsed
    item.last_seen_at = utcnow()
    if normalized.in_stock:
        # The crawler re-reads the newest pages every half hour, so for recent
        # listings this is a far tighter "still on sale" signal than the
        # detail fetches - and it costs nothing, the pages are read anyway.
        item.last_listed_at = item.last_seen_at

    if normalized.detail_loaded:
        # Remember the fetch before this one before overwriting it: without it
        # a copy first seen today has no upper bound on how long it had
        # already been sitting there.
        item.prev_detail_fetch_at = item.last_detail_fetch_at
        item.detail_loaded = True
        item.last_detail_fetch_at = utcnow()
        item.raw = normalized.raw

    # Record which photos exist so they can be served and fetched later. The
    # public route is a hash of the source URL and cannot be reversed, so
    # without this the image endpoint has no idea what to go and get.
    from . import images as image_cache

    image_cache.register(db, image_cache.urls_for_item(item))

    price_moved = normalized.price is not None and normalized.price != previous_price
    stock_moved = normalized.in_stock != previous_stock
    changed = created or price_moved or stock_moved

    if changed:
        _record_point(db, item, normalized)
        _refresh_aggregates(db, item)

    # A list sweep cannot see individual copies, but it can see the price
    # range move, and on a pre-owned product that means the shelf changed.
    # The newest pages are re-read every half hour, so this catches a lot of
    # movement for free - just not all of it: with four copies at one price
    # and one cheaper, selling one of the four leaves the range untouched.
    if not normalized.detail_loaded and item.condition == Condition.preowned:
        range_moved = price_moved or (
            normalized.price_max is not None and normalized.price_max != previous_max
        )
        if range_moved and not created:
            from . import shelfwatch

            shelfwatch.promote(db, item)

    # Only a detail response knows the individual graded copies, so only a
    # detail response can tell which of them have gone.
    if normalized.detail_loaded and normalized.variants:
        from . import shelflife

        db.flush()  # the item needs an id before copies can point at it
        shelflife.reconcile(
            db, item, normalized.variants, observed_at=item.last_detail_fetch_at
        )

    if commit:
        db.commit()
    else:
        db.flush()
    return item, changed


def _record_point(db: Session, item: Item, normalized: NormalizedItem) -> None:
    db.add(
        PricePoint(
            item=item,
            price=normalized.price,
            currency=normalized.currency or item.currency,
            in_stock=normalized.in_stock,
            sale_status=normalized.sale_status,
            condition_grade=normalized.condition_grade or item.condition_grade,
        )
    )


def _refresh_aggregates(db: Session, item: Item) -> None:
    """Keep min/max/avg denormalized so list views need no subquery."""
    if item.id is None:
        # Brand new item: the first observation is all three aggregates.
        item.lowest_price = item.current_price
        item.highest_price = item.current_price
        item.average_price = item.current_price
        return

    row = db.execute(
        select(
            func.min(PricePoint.price),
            func.max(PricePoint.price),
            func.avg(PricePoint.price),
        ).where(PricePoint.item_id == item.id, PricePoint.price.is_not(None))
    ).one()
    low, high, avg = row
    candidates = [v for v in (low, item.current_price) if v is not None]
    item.lowest_price = min(candidates) if candidates else None
    candidates = [v for v in (high, item.current_price) if v is not None]
    item.highest_price = max(candidates) if candidates else None
    item.average_price = float(avg) if avg is not None else item.current_price


def mark_unavailable(db: Session, item: Item, commit: bool = True) -> bool:
    """Record that a listing vanished upstream.

    On AmiAmi a sold-out pre-owned listing is deleted rather than flagged, so
    a 'not found' response is real information and belongs in the history.
    """
    if not item.in_stock and item.order_closed:
        return False
    from . import shelflife

    item.in_stock = False
    item.order_closed = True
    item.last_seen_at = utcnow()
    shelflife.close_all(db, item, observed_at=item.last_seen_at)
    # Phase zero: how long the whole product stood before it sold out. Coarse
    # next to following copies, but it needs nothing we were not already
    # recording, so it can speak for products nobody has ever opened.
    db.add(
        PricePoint(
            item=item,
            price=item.current_price,
            currency=item.currency,
            in_stock=False,
            sale_status="Sold out",
            condition_grade=item.condition_grade,
        )
    )
    db.flush()
    days = shelflife.product_sold_out_days(db, item)
    if days is not None:
        item.sold_out_days = days
        shelflife.refresh_estimates(db, item)
    if commit:
        db.commit()
    return True


def history(db: Session, item_id: int, days: int | None = 365) -> list[PricePoint]:
    stmt = select(PricePoint).where(PricePoint.item_id == item_id)
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = stmt.where(PricePoint.recorded_at >= cutoff)
    return list(db.execute(stmt.order_by(PricePoint.recorded_at)).scalars().all())


def price_stats(db: Session, item_id: int) -> dict:
    row = db.execute(
        select(
            func.min(PricePoint.price),
            func.max(PricePoint.price),
            func.avg(PricePoint.price),
            func.count(PricePoint.id),
        ).where(PricePoint.item_id == item_id, PricePoint.price.is_not(None))
    ).one()
    low, high, avg, count = row
    first = db.execute(
        select(PricePoint.recorded_at)
        .where(PricePoint.item_id == item_id)
        .order_by(PricePoint.recorded_at)
        .limit(1)
    ).scalar_one_or_none()
    return {
        "lowest": low,
        "highest": high,
        "average": float(avg) if avg is not None else None,
        "points": int(count or 0),
        "tracked_since": _aware(first),
    }


def prune_history(db: Session, retention_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = (
        db.query(PricePoint).filter(PricePoint.recorded_at < cutoff).delete(synchronize_session=False)
    )
    db.commit()
    if deleted:
        log.info("Pruned %s price points older than %s days", deleted, retention_days)
    return int(deleted or 0)
