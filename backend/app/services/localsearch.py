"""Searching this instance's own catalogue.

The shop only shows what it will sell you today. This catalogue keeps every
listing it has ever seen, including the pre-owned ones AmiAmi deleted the
moment they sold, which makes it both larger than the shop's own search and
able to answer a question the shop cannot: what has this figure actually cost
over time, and is today cheap by its own standards?

Delisted items are kept in the results deliberately, marked rather than
hidden, because that history is the whole advantage.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import Condition, Item, ItemTag, Tag

log = logging.getLogger(__name__)


@dataclass(slots=True)
class LocalResult:
    items: list[Item]
    total: int
    page: int
    per_page: int
    pages: int


def _keyword_clause(term: str):
    """Match a search term against the fields people actually type."""
    pattern = f"%{term}%"
    return or_(
        Item.name.ilike(pattern),
        Item.name_jp.ilike(pattern),
        Item.code.ilike(pattern),
        Item.maker.ilike(pattern),
        Item.series.ilike(pattern),
        Item.character.ilike(pattern),
    )


def _tag_ids(db: Session, slugs: list[str]) -> list[int]:
    cleaned = [s.strip() for s in slugs if s and s.strip()]
    if not cleaned:
        return []
    return list(db.execute(select(Tag.id).where(Tag.slug.in_(cleaned))).scalars().all())


def _apply_filters(stmt: Select, db: Session, req) -> Select:
    """Everything except tags, which need a join and are handled separately."""
    if req.provider:
        stmt = stmt.where(Item.provider == req.provider)

    term = (req.q or "").strip()
    if term:
        # Several words should all match somewhere, so "miku nendoroid" does
        # not return every Miku ever made.
        for word in term.split():
            stmt = stmt.where(_keyword_clause(word))

    if req.condition == "new":
        stmt = stmt.where(Item.condition == Condition.new)
    elif req.condition == "preowned":
        stmt = stmt.where(Item.condition == Condition.preowned)

    if req.availability == "buyable":
        stmt = stmt.where(
            Item.order_closed.is_(False),
            or_(Item.in_stock.is_(True), Item.is_preorder.is_(True), Item.is_backorder.is_(True)),
        )
    elif req.availability == "in_stock":
        stmt = stmt.where(Item.in_stock.is_(True))
    elif req.availability == "preorder":
        stmt = stmt.where(Item.is_preorder.is_(True))
    elif req.availability == "delisted":
        # The ones the shop has removed. Only this catalogue still has them.
        stmt = stmt.where(Item.order_closed.is_(True))

    if req.min_price is not None:
        stmt = stmt.where(Item.current_price >= req.min_price)
    if req.max_price is not None:
        stmt = stmt.where(Item.current_price <= req.max_price)

    if req.at_lowest_ever:
        # Only possible because delisted listings stay: the lowest ever seen
        # includes prices the shop no longer admits to.
        stmt = stmt.where(
            Item.current_price.is_not(None),
            Item.lowest_price.is_not(None),
            Item.current_price <= Item.lowest_price * 1.001,
        )

    if req.sells_within_days:
        stmt = stmt.where(
            Item.dwell_days.is_not(None), Item.dwell_days <= req.sells_within_days
        )

    for field, value in (
        (Item.maker, req.maker),
        (Item.series, req.series),
        (Item.character, req.character),
    ):
        if value:
            stmt = stmt.where(field.ilike(f"%{value.strip()}%"))

    return stmt


def _apply_tags(stmt: Select, db: Session, req) -> Select | None:
    """Include and exclude by MFC tag. None means the filter can never match."""
    wanted = _tag_ids(db, req.tags)
    if req.tags and not wanted:
        # Every requested tag is unknown here, so nothing can carry them.
        return None

    if wanted:
        stmt = stmt.join(ItemTag, ItemTag.item_id == Item.id).where(ItemTag.tag_id.in_(wanted))
        stmt = stmt.group_by(Item.id)
        if req.tag_mode == "all":
            stmt = stmt.having(func.count(func.distinct(ItemTag.tag_id)) == len(wanted))

    unwanted = _tag_ids(db, req.exclude_tags)
    if unwanted:
        # "None of these", which is what excluding nendoroids while hunting
        # a scale figure actually means.
        blocked = select(ItemTag.item_id).where(ItemTag.tag_id.in_(unwanted))
        stmt = stmt.where(Item.id.not_in(blocked))

    return stmt


def _apply_sort(stmt: Select, sort: str) -> Select:
    """Ordering. Newest first by default, meaning newest to this catalogue."""
    if sort == "oldest":
        return stmt.order_by(Item.first_seen_at.asc(), Item.id.asc())
    if sort == "price_asc":
        return stmt.order_by(Item.current_price.asc().nulls_last(), Item.id.desc())
    if sort == "price_desc":
        return stmt.order_by(Item.current_price.desc().nulls_last(), Item.id.desc())
    if sort == "discount":
        # Largest gap between the shop's own list price and what it asks now.
        return stmt.order_by(
            (Item.current_price / func.nullif(Item.list_price, 0)).asc().nulls_last(),
            Item.id.desc(),
        )
    if sort == "lowest_ever":
        # Closest to the cheapest this catalogue has ever recorded, which is a
        # question the shop cannot answer about its own deleted listings.
        return stmt.order_by(
            (Item.current_price / func.nullif(Item.lowest_price, 0)).asc().nulls_last(),
            Item.id.desc(),
        )
    if sort == "sells_fastest":
        # Shortest typical time on the shelf first. Products with no estimate
        # yet sort last rather than pretending to be instant.
        return stmt.order_by(Item.dwell_days.asc().nulls_last(), Item.id.desc())
    if sort == "sells_slowest":
        return stmt.order_by(Item.dwell_days.desc().nulls_last(), Item.id.desc())
    if sort == "release":
        return stmt.order_by(Item.release_date_parsed.desc().nulls_last(), Item.id.desc())
    # "newest" means newest to us, not the shop's release order.
    return stmt.order_by(Item.first_seen_at.desc(), Item.id.desc())


def search(db: Session, req) -> LocalResult:
    """Run a catalogue search and return one page of items."""
    base = select(Item)
    base = _apply_filters(base, db, req)
    tagged = _apply_tags(base, db, req)
    if tagged is None:
        return LocalResult(items=[], total=0, page=req.page, per_page=req.per_page, pages=0)

    # Count through a subquery so grouping from the tag join does not turn the
    # total into one row per group.
    count_stmt = select(func.count()).select_from(tagged.order_by(None).subquery())
    total = int(db.execute(count_stmt).scalar_one() or 0)

    stmt = _apply_sort(tagged, req.sort)
    stmt = stmt.offset((req.page - 1) * req.per_page).limit(req.per_page)
    items = list(db.execute(stmt).scalars().unique().all())

    pages = max(1, -(-total // req.per_page)) if total else 0
    return LocalResult(items=items, total=total, page=req.page, per_page=req.per_page, pages=pages)


def facet_tags(db: Session, limit: int = 60, kinds: list[str] | None = None) -> list[dict]:
    """Tags worth offering, most used first."""
    stmt = select(Tag).where(Tag.usage_count > 0)
    if kinds:
        stmt = stmt.where(Tag.kind.in_(kinds))
    rows = db.execute(stmt.order_by(Tag.usage_count.desc()).limit(limit)).scalars()
    return [
        {
            "slug": t.slug,
            "name": t.name,
            "kind": t.kind.value,
            "usage_count": t.usage_count,
        }
        for t in rows
    ]


def catalogue_summary(db: Session, provider: str | None = None) -> dict:
    """What the local catalogue holds, for the search page's header."""
    stmt = select(
        func.count(Item.id),
        func.sum(func.cast(Item.order_closed, __import__("sqlalchemy").Integer)),
        func.min(Item.first_seen_at),
    )
    if provider:
        stmt = stmt.where(Item.provider == provider)
    total, delisted, since = db.execute(stmt).one()

    buyable = select(func.count(Item.id)).where(
        Item.order_closed.is_(False),
        or_(Item.in_stock.is_(True), Item.is_preorder.is_(True), Item.is_backorder.is_(True)),
    )
    if provider:
        buyable = buyable.where(Item.provider == provider)

    return {
        "items": int(total or 0),
        "buyable": int(db.execute(buyable).scalar_one() or 0),
        "delisted": int(delisted or 0),
        "tracking_since": since,
    }
