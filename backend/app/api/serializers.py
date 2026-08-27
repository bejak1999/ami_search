"""Turning ORM rows and provider results into API payloads."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Alert,
    AlertDelivery,
    CollectionEntry,
    CostProfile,
    Item,
    User,
    Watch,
    WatchSeenItem,
)
from ..providers import NormalizedItem
from ..schemas import AlertOut, ItemOut
from ..services import fx, landed_cost
from ..services import images as image_cache


def register_images(db: Session, items: list[Item]) -> None:
    """Make sure every photo about to be shown can actually be served.

    The public route is a hash of the source URL and cannot be reversed, so an
    item whose photo was never registered renders a blank frame. Registering a
    whole page at once costs one query, and means anything ever displayed is
    servable from that moment on, rather than only the items the background
    prefetch happened to have reached.
    """
    from ..services import images as cache

    urls: list[str] = []
    for item in items:
        urls.extend(cache.urls_for_item(item))
        urls.extend(u for u in (item.images or []) if u)
    if urls:
        cache.register(db, urls, commit=True)


def _card_image(url: str | None) -> str | None:
    """The photo a grid tile should show.

    The full image, not the thumbnail. AmiAmi's 300px thumbnails are visibly
    soft, and on a page whose entire purpose is judging figures by eye that
    trade is the wrong way round: the bandwidth is cheap once cached locally,
    the detail is not.

    Normalising to one size also stops tiles looking inconsistent depending on
    whether an item was last seen in search results or fetched in detail.
    """
    if not url:
        return url
    return image_cache.full_of(url) or url


def _discount(price: float | None, list_price: float | None) -> float | None:
    if not price or not list_price or list_price <= 0 or price >= list_price:
        return None
    return round((1.0 - price / list_price) * 100.0, 1)


def item_from_normalized(
    db: Session,
    normalized: NormalizedItem,
    user: User | None = None,
    profile: CostProfile | None = None,
    stored: Item | None = None,
) -> ItemOut:
    display_currency = (user.display_currency if user else "EUR") or "EUR"
    landed = None
    if profile is not None:
        breakdown = landed_cost.estimate(
            db,
            normalized.price,
            normalized.currency,
            profile,
            target_currency=display_currency,
            item=stored,
        )
        landed = breakdown.as_dict() if breakdown else None

    return ItemOut(
        id=stored.id if stored else None,
        provider=normalized.provider,
        code=normalized.code,
        name=normalized.name,
        name_jp=normalized.name_jp,
        url=normalized.url,
        currency=normalized.currency,
        price=normalized.price,
        price_max=normalized.price_max,
        variants=normalized.variants,
        list_price=normalized.list_price,
        # Point at the local copy: AmiAmi removes a pre-owned listing's photos
        # when it sells, and by then this row is the only record left.
        image_url=image_cache.public_url(_card_image(normalized.image_url)),
        images=[image_cache.public_url(u) for u in normalized.images if u],
        maker=normalized.maker,
        series=normalized.series,
        character=normalized.character,
        scale=normalized.scale,
        jan_code=normalized.jan_code,
        spec=normalized.spec,
        remarks=normalized.remarks,
        condition=normalized.condition,
        condition_grade=normalized.condition_grade,
        in_stock=normalized.in_stock,
        is_preorder=normalized.is_preorder,
        is_backorder=normalized.is_backorder,
        order_closed=normalized.order_closed,
        release_date=normalized.release_date,
        discount_pct=_discount(normalized.price, normalized.list_price),
        landed=landed,
        display_price=fx.convert(db, normalized.price, normalized.currency, display_currency),
        display_currency=display_currency,
        lowest_price=stored.lowest_price if stored else None,
        highest_price=stored.highest_price if stored else None,
        average_price=stored.average_price if stored else None,
        first_seen_at=stored.first_seen_at if stored else None,
        last_seen_at=stored.last_seen_at if stored else None,
    )


# One definition, in the service layer, because the wishlist and the item
# serializer both depend on the two codes meaning the same figure.
from ..services.catalog import counterpart_code  # noqa: E402


def item_out(
    db: Session,
    item: Item,
    user: User | None = None,
    profile: CostProfile | None = None,
    with_context: bool = False,
    with_counterpart: bool = False,
) -> ItemOut:
    display_currency = (user.display_currency if user else "EUR") or "EUR"
    landed = None
    if profile is not None:
        breakdown = landed_cost.estimate(
            db,
            item.current_price,
            item.currency,
            profile,
            target_currency=display_currency,
            item=item,
        )
        landed = breakdown.as_dict() if breakdown else None

    payload = ItemOut(
        id=item.id,
        provider=item.provider,
        code=item.code,
        name=item.name,
        name_jp=item.name_jp,
        url=item.product_url,
        currency=item.currency,
        price=item.current_price,
        price_max=item.price_max,
        variants=item.variants or [],
        dwell_days=item.dwell_days,
        dwell_basis=item.dwell_basis,
        dwell_samples=item.dwell_samples or 0,
        listing_count=item.listing_count or 0,
        list_price=item.list_price,
        image_url=image_cache.public_url(_card_image(item.image_url)),
        images=[image_cache.public_url(u) for u in (item.images or []) if u],
        maker=item.maker,
        series=item.series,
        character=item.character,
        scale=item.scale,
        jan_code=item.jan_code,
        spec=item.spec,
        remarks=item.remarks,
        condition=item.condition.value,
        condition_grade=item.condition_grade,
        in_stock=item.in_stock,
        is_preorder=item.is_preorder,
        is_backorder=item.is_backorder,
        order_closed=item.order_closed,
        release_date=item.release_date,
        discount_pct=_discount(item.current_price, item.list_price),
        landed=landed,
        display_price=fx.convert(db, item.current_price, item.currency, display_currency),
        display_currency=display_currency,
        lowest_price=item.lowest_price,
        highest_price=item.highest_price,
        average_price=item.average_price,
        first_seen_at=item.first_seen_at,
        last_seen_at=item.last_seen_at,
    )

    payload.mfc_id = item.mfc_id
    payload.mfc_url = item.mfc_url
    payload.mfc_matched_by = item.mfc_matched_by
    payload.mfc_confidence = item.mfc_confidence
    payload.mfc_restricted = bool(item.mfc_restricted)

    if with_counterpart:
        other = db.execute(
            select(Item).where(
                Item.provider == item.provider, Item.code == counterpart_code(item.code)
            )
        ).scalar_one_or_none()
        if other is not None:
            payload.counterpart = {
                "id": other.id,
                "code": other.code,
                "condition": other.condition.value,
                "price": other.current_price,
                "currency": other.currency,
                "in_stock": other.in_stock,
            }

    if with_context and user is not None:
        payload.tags = [
            {"kind": t.kind.value, "slug": t.slug, "name": t.name, "is_auto": t.is_auto}
            for t in sorted(item.tags, key=lambda t: (t.kind.value, t.name))
        ]
        payload.watch_count = int(
            db.execute(
                select(func.count(WatchSeenItem.id))
                .join(Watch, Watch.id == WatchSeenItem.watch_id)
                .where(Watch.user_id == user.id, WatchSeenItem.item_id == item.id)
            ).scalar_one()
            or 0
        )
        payload.tracked = payload.watch_count > 0
        # A wishlist entry is about the figure, not about the one listing you
        # happened to click. The same figure is sold under two codes - the
        # pre-owned one carries an -R suffix - so having saved either of them
        # counts as having saved the figure, and the heart has to show that
        # from both sides.
        entry = db.execute(
            select(CollectionEntry)
            .join(Item, Item.id == CollectionEntry.item_id)
            .where(
                CollectionEntry.user_id == user.id,
                Item.provider == item.provider,
                Item.code.in_([item.code, counterpart_code(item.code)]),
            )
            # The listing actually clicked wins when both are saved.
            .order_by((Item.code == item.code).desc())
        ).scalars().first()
        payload.in_collection = entry.status.value if entry else None
        payload.collection_entry_id = entry.id if entry else None
        payload.saved_via_counterpart = bool(entry) and entry.item_id != item.id
    return payload


def alert_out(db: Session, alert: Alert) -> AlertOut:
    payload = AlertOut.model_validate(alert)
    # Alerts keep the origin URL so outbound notifications work even when the
    # instance is not reachable from the internet, but the UI reads from the
    # local copy, which outlives the listing.
    payload.image_url = image_cache.public_url(alert.image_url)
    if alert.watch is not None:
        payload.watch_label = alert.watch.label or alert.watch.query or alert.watch.item_code
    rows = db.execute(
        select(AlertDelivery.status, func.count(AlertDelivery.id))
        .where(AlertDelivery.alert_id == alert.id)
        .group_by(AlertDelivery.status)
    ).all()
    payload.delivery_summary = {status.value: int(count) for status, count in rows}
    return payload
