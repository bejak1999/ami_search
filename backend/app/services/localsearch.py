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

from ..models import Condition, Item, ItemTag, Listing, ListingStatus, Tag

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

    terms = [] if getattr(req, "ignore_blocklist", False) else (
        getattr(req, "blocked_terms", None) or []
    )
    for term in terms:
        cleaned = str(term).strip()
        if cleaned:
            # Matched against the name only. Blocking "nendoroid" should not
            # also lose a scale figure whose maker happens to be Good Smile.
            stmt = stmt.where(~Item.name.ilike(f"%{cleaned}%"))

    if req.sells_within_days:
        stmt = stmt.where(
            Item.dwell_days.is_not(None), Item.dwell_days <= req.sells_within_days
        )

    for field, value in (
        (Item.maker, req.maker),
        (Item.series, req.series),
        (Item.character, req.character),
    ):
        # A Discover rail draws on every series or character on your list, so
        # these arrive as lists. A bare string is still accepted, because a
        # hand-written link or an older client will send one.
        wanted = [value] if isinstance(value, str) else list(value or [])
        terms = [term.strip() for term in wanted if term and term.strip()]
        if terms:
            stmt = stmt.where(or_(*[field.ilike(f"%{term}%") for term in terms]))

    if req.below_average_ratio:
        # The same test the "below their usual price" rail applies, so its
        # "see all" shows that rail rather than the whole catalogue sorted.
        stmt = stmt.where(
            Item.average_price.is_not(None),
            Item.average_price > 0,
            Item.current_price.is_not(None),
            Item.current_price <= Item.average_price * req.below_average_ratio,
        )

    if req.min_discount_pct:
        stmt = stmt.where(
            Item.list_price.is_not(None),
            Item.list_price > 0,
            Item.current_price.is_not(None),
            Item.current_price <= Item.list_price * (1.0 - req.min_discount_pct / 100.0),
        )

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

    excluded = list(req.exclude_tags or [])
    # Whatever the profile blocks outright, unless this search says otherwise.
    if not getattr(req, "ignore_blocklist", False):
        excluded += list(getattr(req, "blocked_tags", None) or [])

    unwanted = _tag_ids(db, excluded)
    if unwanted:
        # "None of these", which is what excluding nendoroids while hunting
        # a scale figure actually means.
        blocked = select(ItemTag.item_id).where(ItemTag.tag_id.in_(unwanted))
        stmt = stmt.where(Item.id.not_in(blocked))

    stmt = _apply_grades(stmt, req)

    return stmt


def _apply_grades(stmt: Select, req) -> Select:
    """Only items with a copy on sale in the condition asked for.

    AmiAmi grades used stock S, A, B+, B, C, D - best first - and grades the
    figure and its box separately, because a mint figure in a crushed box is
    common and priced accordingly.

    Exact by default rather than a floor. Someone asking for C is usually
    asking for the cheap ones, and treating it as "C or better" would bury
    them under every nicer copy in the catalogue - the opposite of what was
    wanted. ``grade_or_better`` turns it back into a floor for the case where
    a minimum is what was meant.

    Matched against individual copies rather than the product, because that is
    where the grade lives. A product with nine copies has nine conditions, and
    the product-level grade is only ever the cheapest one's - filtering on it
    would drop a product whose dear copy is exactly what was asked for. When
    both filters are given they must hold for the *same* copy: an A figure and
    an A box on two different copies is not an A copy.

    Only copies still on sale count. A grade filter is a shopping question.
    """
    from ..providers.amiami import GRADE_ORDER

    wanted_item = getattr(req, "item_grade", None)
    wanted_box = getattr(req, "box_grade", None)
    if not wanted_item and not wanted_box:
        return stmt

    or_better = bool(getattr(req, "grade_or_better", False))

    def acceptable(grade: str) -> list[str]:
        """The grades this filter admits."""
        if not or_better:
            return [grade]
        return list(GRADE_ORDER[: GRADE_ORDER.index(grade) + 1])

    conditions = [Listing.item_id == Item.id, Listing.status == ListingStatus.live]
    if wanted_item:
        conditions.append(Listing.item_grade.in_(acceptable(wanted_item)))
    if wanted_box:
        conditions.append(Listing.box_grade.in_(acceptable(wanted_box)))

    return stmt.where(select(Listing.id).where(*conditions).exists())


def _apply_sort(stmt: Select, sort: str) -> Select:
    """Ordering. Newest first by default, meaning newest to this catalogue."""
    if sort == "newest_copy":
        # Newly on sale second-hand, which is not the same question as newly
        # known here: a product we have had for months can take a copy in
        # today. Products that have never had one sort last rather than
        # pretending to be old.
        return stmt.order_by(Item.last_listing_at.desc().nulls_last(), Item.id.desc())
    if sort == "oldest":
        return stmt.order_by(Item.first_seen_at.asc(), Item.id.asc())
    if sort == "price_asc":
        return stmt.order_by(Item.current_price.asc().nulls_last(), Item.id.desc())
    if sort == "price_desc":
        return stmt.order_by(Item.current_price.desc().nulls_last(), Item.id.desc())
    if sort == "discount":
        # Furthest below the shop's own list price, as a share of it.
        return stmt.order_by(
            (Item.current_price / func.nullif(Item.list_price, 0)).asc().nulls_last(),
            Item.id.desc(),
        )
    if sort == "saving":
        # The same comparison read as money rather than as a proportion. A
        # different question with a different answer: the biggest percentage
        # off is usually a cheap figure, the biggest sum saved an expensive
        # one, and neither ranking is a substitute for the other.
        return stmt.order_by(
            (Item.list_price - Item.current_price).desc().nulls_last(),
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
    if sort == "changed":
        # Last time the shop moved something about this listing, rather than
        # when it first reached us. This is what "recently updated" means on
        # the shop's own site, and the closest thing our data has to it.
        return stmt.order_by(Item.last_seen_at.desc(), Item.id.desc())
    # "newest" means newest to us, not the shop's release order.
    return stmt.order_by(Item.first_seen_at.desc(), Item.id.desc())


def _collapse_conditions(stmt: Select) -> Select:
    """One row per figure instead of one per listing.

    The same figure is sold new and pre-owned under two codes, so a search
    otherwise returns it twice. Which of the pair survives is the one someone
    would actually click: in stock beats out of stock, and then cheaper wins.
    """
    ranked = (
        stmt.add_columns(
            func.row_number()
            .over(
                partition_by=Item.figure_code,
                order_by=[
                    Item.in_stock.desc(),
                    Item.order_closed.asc(),
                    Item.current_price.asc().nulls_last(),
                    Item.id.desc(),
                ],
            )
            .label("rank")
        )
        .order_by(None)
        .subquery()
    )
    return select(Item).join(ranked, ranked.c.id == Item.id).where(ranked.c.rank == 1)


def search(db: Session, req) -> LocalResult:
    """Run a catalogue search and return one page of items."""
    base = select(Item)
    base = _apply_filters(base, db, req)
    tagged = _apply_tags(base, db, req)
    if tagged is None:
        return LocalResult(items=[], total=0, page=req.page, per_page=req.per_page, pages=0)

    if getattr(req, "combine_conditions", False):
        tagged = _collapse_conditions(tagged)

    # Count through a subquery so grouping from the tag join does not turn the
    # total into one row per group.
    count_stmt = select(func.count()).select_from(tagged.order_by(None).subquery())
    total = int(db.execute(count_stmt).scalar_one() or 0)

    stmt = _apply_sort(tagged, req.sort)
    stmt = stmt.offset((req.page - 1) * req.per_page).limit(req.per_page)
    items = list(db.execute(stmt).scalars().unique().all())

    pages = max(1, -(-total // req.per_page)) if total else 0
    return LocalResult(items=items, total=total, page=req.page, per_page=req.per_page, pages=pages)


def facet_tags(
    db: Session,
    limit: int = 60,
    kinds: list[str] | None = None,
    q: str | None = None,
) -> list[dict]:
    """Tags worth offering, most used first.

    The search term has to be applied here rather than to the returned page.
    Filtering afterwards searches only the most-used handful, which looked
    right while there were a few hundred tags in total and quietly stopped
    working as MyFigureCollection linking filled the table: a tag outside the
    top of the usage list became unfindable, however exactly it was typed.
    """
    stmt = select(Tag).where(Tag.usage_count > 0)
    if kinds:
        stmt = stmt.where(Tag.kind.in_(kinds))
    if q and q.strip():
        # Escaped, because a tag search for "1/7" or "50%" is an ordinary
        # thing to type and LIKE would otherwise read those as wildcards.
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{needle}%"
        stmt = stmt.where(
            or_(
                Tag.name.ilike(pattern, escape="\\"),
                Tag.slug.ilike(pattern, escape="\\"),
            )
        )
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
