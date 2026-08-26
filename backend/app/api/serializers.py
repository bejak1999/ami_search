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
        image_url=normalized.image_url,
        images=normalized.images,
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


def counterpart_code(code: str) -> str:
    """The other condition's product code for the same figure.

    AmiAmi lists a pre-owned copy under the new code with an ``-R`` suffix, so
    the relationship is purely mechanical.
    """
    return code[:-2] if code.endswith("-R") else code + "-R"


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
        list_price=item.list_price,
        image_url=item.image_url,
        images=item.images or [],
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
        entry = db.execute(
            select(CollectionEntry).where(
                CollectionEntry.user_id == user.id, CollectionEntry.item_id == item.id
            )
        ).scalar_one_or_none()
        payload.in_collection = entry.status.value if entry else None
        payload.collection_entry_id = entry.id if entry else None
    return payload


def alert_out(db: Session, alert: Alert) -> AlertOut:
    payload = AlertOut.model_validate(alert)
    if alert.watch is not None:
        payload.watch_label = alert.watch.label or alert.watch.query or alert.watch.item_code
    rows = db.execute(
        select(AlertDelivery.status, func.count(AlertDelivery.id))
        .where(AlertDelivery.alert_id == alert.id)
        .group_by(AlertDelivery.status)
    ).all()
    payload.delivery_summary = {status.value: int(count) for status, count in rows}
    return payload
