"""The discovery feed.

Filtering only answers a question you already had. This page is for the other
case: showing things worth knowing about before you thought to look.

Every rail is built from what this instance already holds, so the page has
something to say the moment it opens rather than waiting for input. The rails
that depend on the user's own watches and wishlist come first when there is
anything to fill them, because a suggestion drawn from what someone already
wants beats a generic new-arrivals list.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.orm import Session

from ..models import (
    CollectionEntry,
    CollectionStatus,
    Condition,
    Item,
    ItemTag,
    User,
    Watch,
    WatchSeenItem,
)

log = logging.getLogger(__name__)

MAX_PER_RAIL = 18


@dataclass(slots=True)
class Rail:
    key: str
    title: str
    subtitle: str
    icon: str
    items: list[Item] = field(default_factory=list)
    #: Search parameters that reproduce this rail in full.
    explore: dict | None = None


def _buyable(stmt: Select) -> Select:
    """Only things that can actually be bought right now.

    The catalogue deliberately keeps sold-out listings, which is right for
    price research and wrong for a page whose whole purpose is suggesting
    something to buy.
    """
    return stmt.where(
        Item.order_closed.is_(False),
        Item.current_price.is_not(None),
        or_(Item.in_stock.is_(True), Item.is_preorder.is_(True), Item.is_backorder.is_(True)),
    )


def _taste(db: Session, user: User) -> dict[str, list]:
    """What this person has shown an interest in.

    Read from wishlists and from what their watches have matched, since both
    are explicit statements of interest rather than inference from browsing.
    """
    item_ids = set(
        db.execute(
            select(CollectionEntry.item_id).where(CollectionEntry.user_id == user.id)
        )
        .scalars()
        .all()
    ) | set(
        db.execute(
            select(WatchSeenItem.item_id)
            .join(Watch, Watch.id == WatchSeenItem.watch_id)
            .where(Watch.user_id == user.id, Watch.enabled.is_(True))
        )
        .scalars()
        .all()
    )

    if not item_ids:
        return {"item_ids": [], "series": [], "characters": [], "makers": [], "tag_ids": []}

    rows = db.execute(select(Item).where(Item.id.in_(item_ids))).scalars().all()
    series = {r.series for r in rows if r.series}
    characters = {r.character for r in rows if r.character}
    makers = {r.maker for r in rows if r.maker}

    tag_ids = list(
        db.execute(
            select(ItemTag.tag_id, func.count(ItemTag.item_id).label("n"))
            .where(ItemTag.item_id.in_(item_ids))
            .group_by(ItemTag.tag_id)
            .order_by(func.count(ItemTag.item_id).desc())
            .limit(25)
        )
        .scalars()
        .all()
    )

    return {
        "item_ids": list(item_ids),
        "series": sorted(series),
        "characters": sorted(characters),
        "makers": sorted(makers),
        "tag_ids": tag_ids,
    }


def _fetch(db: Session, stmt: Select, exclude: set[int], limit: int = MAX_PER_RAIL) -> list[Item]:
    """Run a rail's query, skipping anything already shown or already owned."""
    if exclude:
        stmt = stmt.where(Item.id.not_in(exclude))
    return list(db.execute(stmt.limit(limit)).scalars().unique().all())


def build(db: Session, user: User, provider: str | None = None) -> list[Rail]:
    """Assemble the feed. Empty rails are dropped rather than shown bare."""
    taste = _taste(db, user)
    owned = set(
        db.execute(
            select(CollectionEntry.item_id).where(
                CollectionEntry.user_id == user.id,
                CollectionEntry.status.in_([CollectionStatus.owned, CollectionStatus.sold]),
            )
        )
        .scalars()
        .all()
    )
    seen: set[int] = set(owned)
    rails: list[Rail] = []

    def base() -> Select:
        stmt = _buyable(select(Item))
        return stmt.where(Item.provider == provider) if provider else stmt

    # --- drawn from what the person already wants -------------------------
    if taste["series"]:
        stmt = base().where(Item.series.in_(taste["series"][:40])).order_by(
            Item.first_seen_at.desc()
        )
        items = _fetch(db, stmt, seen | set(taste["item_ids"]))
        if items:
            seen.update(i.id for i in items)
            rails.append(
                Rail(
                    key="your_series",
                    title="From series you follow",
                    subtitle="New to the catalogue, from series already on your list",
                    icon="sparkle",
                    items=items,
                    explore={"series": taste["series"][0]},
                )
            )

    if taste["characters"]:
        stmt = base().where(Item.character.in_(taste["characters"][:40])).order_by(
            Item.current_price.asc().nulls_last()
        )
        items = _fetch(db, stmt, seen | set(taste["item_ids"]))
        if items:
            seen.update(i.id for i in items)
            rails.append(
                Rail(
                    key="your_characters",
                    title="Characters you collect",
                    subtitle="Cheapest first",
                    icon="heart",
                    items=items,
                    explore={"character": taste["characters"][0]},
                )
            )

    if taste["tag_ids"]:
        # Figures sharing the descriptive tags of things already wanted, which
        # is the only rail MyFigureCollection makes possible.
        stmt = (
            _buyable(select(Item))
            .join(ItemTag, ItemTag.item_id == Item.id)
            .where(ItemTag.tag_id.in_(taste["tag_ids"]))
            .group_by(Item.id)
            .order_by(func.count(ItemTag.tag_id).desc(), Item.first_seen_at.desc())
        )
        items = _fetch(db, stmt, seen | set(taste["item_ids"]))
        if items:
            seen.update(i.id for i in items)
            rails.append(
                Rail(
                    key="your_tags",
                    title="Like the ones you saved",
                    subtitle="Sharing the most MyFigureCollection tags with your list",
                    icon="tag",
                    items=items,
                )
            )

    # --- rails that work on an empty account too --------------------------
    stmt = (
        base()
        .where(Item.condition == Condition.preowned)
        .order_by(Item.first_seen_at.desc())
    )
    items = _fetch(db, stmt, seen)
    if items:
        seen.update(i.id for i in items)
        rails.append(
            Rail(
                key="new_preowned",
                title="Just listed pre-owned",
                subtitle="These sell fastest, and vanish without warning when they do",
                icon="fire",
                items=items,
                explore={"condition": "preowned", "sort": "newest"},
            )
        )

    # Things that will not wait for you. The "just listed" rail above claims
    # pre-owned sells fastest as a generality; this one is the products where
    # we have actually measured it.
    stmt = (
        base()
        .where(
            Item.dwell_days.is_not(None),
            Item.dwell_days <= 7.0,
            Item.condition == Condition.preowned,
        )
        .order_by(Item.dwell_days.asc(), Item.first_seen_at.desc())
    )
    items = _fetch(db, stmt, seen)
    if items:
        seen.update(i.id for i in items)
        rails.append(
            Rail(
                key="sells_fast",
                title="Gone within a week",
                subtitle="Copies of these have measurably short shelf lives here",
                icon="clock",
                items=items,
                explore={"condition": "preowned", "sort": "sells_fastest"},
            )
        )

    # Cheap against their own history, which only works because delisted
    # listings keep their last price here.
    stmt = (
        base()
        .where(
            Item.lowest_price.is_not(None),
            Item.average_price.is_not(None),
            Item.average_price > 0,
            Item.current_price <= Item.average_price * 0.85,
        )
        .order_by((Item.current_price / func.nullif(Item.average_price, 0)).asc())
    )
    items = _fetch(db, stmt, seen)
    if items:
        seen.update(i.id for i in items)
        rails.append(
            Rail(
                key="below_usual",
                title="Below their usual price",
                subtitle="Measured against every price recorded here, not the shop's list price",
                icon="down",
                items=items,
                explore={"sort": "lowest_ever"},
            )
        )

    stmt = base().where(
        Item.list_price.is_not(None),
        Item.list_price > 0,
        Item.current_price <= Item.list_price * 0.6,
    ).order_by((Item.current_price / func.nullif(Item.list_price, 0)).asc())
    items = _fetch(db, stmt, seen)
    if items:
        seen.update(i.id for i in items)
        rails.append(
            Rail(
                key="heavily_discounted",
                title="Well under list price",
                subtitle="At least 40% off what the maker asked",
                icon="yen",
                items=items,
                explore={"sort": "discount"},
            )
        )

    stmt = base().where(Item.in_stock.is_(True)).order_by(Item.last_seen_at.desc())
    items = _fetch(db, stmt, seen)
    if items:
        rails.append(
            Rail(
                key="in_stock",
                title="In stock right now",
                subtitle="Ready to ship today",
                icon="box",
                items=items,
                explore={"availability": "in_stock"},
            )
        )

    return rails
