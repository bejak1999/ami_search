"""Telling four different price movements apart.

A pre-owned product on AmiAmi is not one price. It is several separately
graded copies under one product code, and the price we show is the cheapest
of them. That number falls for reasons that mean completely different things
to someone watching a figure:

    the copy you were watching was marked down     - a real reduction
    a cheaper copy arrived beside it               - possibly a rougher one
    the cheap copy sold and the next one is dearer - nothing got dearer

Comparing product prices alone cannot separate those, because all three are
just a number moving. Remembering *which copy* was cheapest can, and the
listing rows already record exactly that.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, Listing, ListingStatus, PriceCheck, PricePoint, utcnow

#: The copy someone was watching is now cheaper than it was.
MARKDOWN = "markdown"
#: A different, cheaper copy has appeared beside it.
UNDERCUT = "undercut"
#: The cheapest copy has gone and the next one up costs more.
SOLD_OUT_CHEAPEST = "sold_out_cheapest"
#: The same copy is being asked more for than it was.
INCREASE = "increase"
#: Cheaper, but we cannot say which copy - a new figure, or a non-graded one.
CHEAPER = "cheaper"
#: Dearer, likewise without a copy to point at.
DEARER = "dearer"
#: There is nothing buyable under this code any more.
UNAVAILABLE = "unavailable"

#: Movements the interface paints red. Both upward cases count, by choice:
#: the practical question is "does this cost me more than last time", and it
#: does either way. The label still says which of the two happened.
UPWARD = frozenset({SOLD_OUT_CHEAPEST, INCREASE, DEARER, UNAVAILABLE})


def baseline_for(db: Session, user_id: int, item_id: int) -> PriceCheck | None:
    return db.execute(
        select(PriceCheck).where(
            PriceCheck.user_id == user_id, PriceCheck.item_id == item_id
        )
    ).scalar_one_or_none()


#: SQLite refuses a statement with more than 32,766 bound parameters, and the
#: failure is a hard error rather than a slow query. A collection large enough
#: to reach it is unusual but not impossible, and the page that would break is
#: the one someone looks at most.
_ID_CHUNK = 5_000


def baselines_for(db: Session, user_id: int, item_ids: list[int]) -> dict[int, PriceCheck]:
    """Every stored check for these items, keyed by item id."""
    found: dict[int, PriceCheck] = {}
    for start in range(0, len(item_ids), _ID_CHUNK):
        chunk = item_ids[start : start + _ID_CHUNK]
        rows = db.execute(
            select(PriceCheck).where(
                PriceCheck.user_id == user_id, PriceCheck.item_id.in_(chunk)
            )
        ).scalars()
        found.update({row.item_id: row for row in rows})
    return found


def cheapest_copy(db: Session, item: Item) -> Listing | None:
    """The live copy that sets the product's price, if we know its copies.

    ``last_price`` rather than ``price``: the first is what the copy costs
    now, the second what it cost when we first saw it, and used stock is
    marked down while it waits.
    """
    rows = [
        row
        for row in db.execute(
            select(Listing).where(
                Listing.item_id == item.id, Listing.status == ListingStatus.live
            )
        ).scalars()
        if (row.last_price if row.last_price is not None else row.price) is not None
    ]
    if not rows:
        return None
    return min(rows, key=lambda r: r.last_price if r.last_price is not None else r.price)


def copy_is_live(db: Session, item: Item, code: str | None) -> bool:
    if not code:
        return False
    row = db.execute(
        select(Listing.status).where(
            Listing.provider == item.provider, Listing.code == code
        )
    ).scalar_one_or_none()
    return row == ListingStatus.live


def standing_since(db: Session, item: Item) -> datetime | None:
    """When the price we currently hold was first recorded.

    Used only for the first check of a figure, where there is no baseline to
    look back from. Saying "it had been 10,000 since the 12th" is the most
    the history can honestly support at that point.
    """
    return db.execute(
        select(PricePoint.recorded_at)
        .where(
            PricePoint.item_id == item.id,
            PricePoint.listing_id.is_(None),
            PricePoint.price.is_not(None),
        )
        .order_by(PricePoint.recorded_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def classify(
    *,
    reference_price: float | None,
    reference_code: str | None,
    now_price: float | None,
    now_code: str | None,
    reference_copy_still_live: bool,
) -> str | None:
    """Which of the movements this is, or None when nothing moved.

    Kept free of the database so the decision table can be tested directly
    rather than through a shop fetch.
    """
    if now_price is None:
        return UNAVAILABLE if reference_price is not None else None
    if reference_price is None:
        return None

    if now_price < reference_price:
        if now_code and reference_code and now_code == reference_code:
            return MARKDOWN
        if now_code and reference_code and now_code != reference_code:
            return UNDERCUT
        return CHEAPER

    if now_price > reference_price:
        if now_code and reference_code and now_code == reference_code:
            return INCREASE
        # The copy that set the old price is gone and the price went up with
        # it. Nobody raised anything; the cheap one sold.
        if reference_code and not reference_copy_still_live:
            return SOLD_OUT_CHEAPEST
        return DEARER

    return None


def remember(
    db: Session,
    *,
    user_id: int,
    item: Item,
    kind: str | None,
    reference_price: float | None,
    reference_code: str | None,
    now_price: float | None,
    now_code: str | None,
    now_grade: str | None,
    since: datetime | None,
) -> PriceCheck:
    """Move the fixed point forward, and record what the move was."""
    row = baseline_for(db, user_id, item.id)
    if row is None:
        row = PriceCheck(user_id=user_id, item_id=item.id)
        db.add(row)

    row.checked_at = utcnow()
    row.price = now_price
    row.currency = item.currency or "JPY"
    row.cheapest_code = now_code
    row.cheapest_grade = now_grade
    row.previous_price = reference_price
    row.previous_code = reference_code
    row.previous_since = since
    row.kind = kind
    return row


def as_payload(row: PriceCheck | None) -> dict | None:
    """What the interface needs to colour a price and explain the colour."""
    if row is None or row.kind is None:
        return None

    was, now = row.previous_price, row.price
    difference = percent = None
    if was is not None and now is not None:
        difference = round(now - was, 2)
        if was:
            percent = round((now - was) / was * 100, 1)

    return {
        "kind": row.kind,
        "direction": "up" if row.kind in UPWARD else "down",
        "was": was,
        "now": now,
        "difference": difference,
        "percent": percent,
        "currency": row.currency,
        "copy_code": row.cheapest_code,
        "copy_grade": row.cheapest_grade,
        "previous_code": row.previous_code,
        "checked_at": row.checked_at,
        "since": row.previous_since,
    }
