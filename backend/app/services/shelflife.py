"""How long a copy stays on the shelf before someone buys it.

AmiAmi sells a used figure as several separately graded copies under one
product code, and deletes a copy outright when it sells. Until now those
copies lived only in ``Item.variants``, a JSON blob overwritten on every
detail fetch, so a sale left no trace whatsoever. This module gives each copy
a row, notices when one disappears, and turns that into the number a buyer
actually wants: is this the kind of figure that sits for a month, or the kind
that is gone by Thursday?

Two independent estimators, because they fail in different places:

**Observed** — follow individual copies. Precise, attributes by grade and
price, but it only knows what it watched happen, so it needs weeks of history
per product and a poll rate fast enough to bracket a sale.

**Intake** — AmiAmi numbers copies per product in the order it takes them in,
so ``FIGURE-140238-R459`` is the 459th used copy of that figure. Watching that
counter climb gives the arrival rate without following anything. Arrival rate
plus shelf depth is all Little's Law needs:

    W = L / lambda        mean time on shelf = copies listed / copies per day

That works from two observations weeks apart and covers the whole catalogue,
where the observed method would need a detail fetch every few hours.

Everything here is an interval, never a point. We see a copy only when we
happen to look, so a lifetime is bracketed by the polls either side of it and
the UI has to say so. :func:`lifetime_of` returns both bounds and flags when
the upper one is open.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Item,
    Listing,
    ListingOutcome,
    ListingStatus,
    PricePoint,
    utcnow,
)

log = logging.getLogger(__name__)

#: FIGURE-140238-R459 -> 459. The bare product code FIGURE-140238-R has no
#: number and is not a copy, so it deliberately does not match.
_SEQUENCE_RE = re.compile(r"-R(\d+)$", re.IGNORECASE)

#: Below this many completed lifetimes the observed median is too thin to lead
#: with, and the intake estimate takes over as the headline figure.
MIN_OBSERVED_SAMPLES = 3

#: Two intake observations closer together than this say nothing useful about
#: a rate, so the bootstrap estimate keeps the floor until the window opens up.
MIN_INTAKE_WINDOW_DAYS = 3.0

#: When this many copies vanish at once and take the whole shelf with them, a
#: shop-side action is at least as likely as that many simultaneous sales.
BATCH_WITHDRAWAL_SIZE = 3

#: Guard rails for a derived figure, so one odd product cannot render as
#: "sells in 4 minutes" or "sells in 90 years".
MIN_DWELL_DAYS = 0.25
MAX_DWELL_DAYS = 1825.0


def sequence_of(code: str | None) -> int | None:
    """The per-product intake number encoded in a copy's code."""
    match = _SEQUENCE_RE.search((code or "").strip())
    return int(match.group(1)) if match else None


def product_code_of(code: str | None) -> str:
    """Reduce a copy's code to the product it belongs to.

    FIGURE-140238-R459 is one copy; FIGURE-140238-R is the product every copy
    of it hangs off. Watches are stored against the product, so anything
    matching a watch to an item has to reduce in this direction first.
    """
    return _SEQUENCE_RE.sub("-R", (code or "").strip())


def _as_aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; comparisons need them aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _days_between(start: datetime | None, end: datetime | None) -> float | None:
    start, end = _as_aware(start), _as_aware(end)
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds() / 86400.0)


# ---------------------------------------------------------------------------
# Lifetimes
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Lifetime:
    """What we can and cannot say about one copy's time on the shelf.

    ``certain_days`` is the span we actually witnessed. ``max_days`` is the
    most it could have been given when we last looked and did not see it. When
    the copy was already there the first time we looked at the product, its
    start is unknown and ``open_start`` is set: the honest phrasing is then
    "at least N days", never a bare number.
    """

    certain_days: float
    max_days: float | None
    open_start: bool
    open_end: bool
    observations: int

    @property
    def is_complete(self) -> bool:
        """True once the copy has actually left the shelf."""
        return not self.open_end

    def as_dict(self) -> dict:
        return {
            "certain_days": round(self.certain_days, 2),
            "max_days": round(self.max_days, 2) if self.max_days is not None else None,
            "open_start": self.open_start,
            "open_end": self.open_end,
            "observations": self.observations,
        }


def lifetime_of(listing: Listing, now: datetime | None = None) -> Lifetime:
    """Bracket how long ``listing`` has been, or was, on the shelf."""
    now = _as_aware(now) or utcnow()
    open_end = listing.status == ListingStatus.live
    end_certain = _as_aware(listing.last_seen_at)
    if open_end:
        # Still listed: it has certainly been there until now.
        end_certain = now

    certain = _days_between(listing.first_seen_at, end_certain) or 0.0

    open_start = listing.appeared_after is None
    if open_start or open_end:
        max_days = None
    else:
        max_days = _days_between(listing.appeared_after, listing.vanished_before)
    return Lifetime(
        certain_days=certain,
        max_days=max_days,
        open_start=open_start,
        open_end=open_end,
        observations=listing.observations or 1,
    )


# ---------------------------------------------------------------------------
# Survival statistics
# ---------------------------------------------------------------------------


def kaplan_meier_median(samples: list[tuple[float, bool]]) -> float | None:
    """Median lifetime from observations where some have not ended yet.

    Averaging only the copies you watched sell is the classic way to get this
    wrong: the slow ones are still sitting on the shelf, so they never enter
    the average and the answer comes out far too optimistic. Kaplan-Meier
    keeps them in as "lasted at least this long", which is exactly what they
    tell us.

    ``samples`` is (duration in days, did it actually end). Returns the first
    time the survival curve reaches one half, or None when it never does -
    with mostly still-listed copies the median genuinely is not known yet, and
    inventing one would be worse than admitting that.
    """
    if not samples:
        return None

    ordered = sorted(samples, key=lambda s: s[0])
    at_risk = len(ordered)
    survival = 1.0
    index = 0

    while index < len(ordered):
        time = ordered[index][0]
        # Everything recorded at this exact time resolves together.
        tied = [s for s in ordered[index:] if s[0] == time]
        events = sum(1 for _, ended in tied if ended)
        if events and at_risk > 0:
            survival *= 1.0 - events / at_risk
            if survival <= 0.5:
                return time
        at_risk -= len(tied)
        index += len(tied)

    return None


def _samples_for(listings: list[Listing], now: datetime | None = None) -> list[tuple[float, bool]]:
    out: list[tuple[float, bool]] = []
    for listing in listings:
        life = lifetime_of(listing, now)
        if life.open_start and life.is_complete:
            # We never saw it arrive, so its true length is unknown and larger
            # than what we measured. Treating it as a completed observation
            # would drag the median down; as a censored one it still counts.
            out.append((life.certain_days, False))
        else:
            out.append((life.certain_days, life.is_complete))
    return out


# ---------------------------------------------------------------------------
# Intake counter
# ---------------------------------------------------------------------------


def intake_rate(item: Item) -> tuple[float | None, str | None]:
    """Copies taken in per day, and how we arrived at the figure.

    The reliable form needs the counter observed twice far enough apart. Until
    then the product's age since release stands in for the elapsed time, which
    assumes the counter started at release: it did not, intake begins later,
    so the bootstrap under-states the rate and over-states the shelf time. It
    is a starting point, not an answer, and it is labelled as one.
    """
    first_seq, last_seq = item.intake_first_seq, item.intake_last_seq
    first_at, last_at = item.intake_first_at, item.intake_last_at

    if None not in (first_seq, last_seq, first_at, last_at):
        window = _days_between(first_at, last_at) or 0.0
        gained = (last_seq or 0) - (first_seq or 0)
        if window >= MIN_INTAKE_WINDOW_DAYS and gained > 0:
            return gained / window, "measured"

    # Bootstrap: total copies ever taken in, spread over the product's life.
    seq = last_seq if last_seq is not None else first_seq
    released = _as_aware(item.release_date_parsed)
    if seq and released:
        age = _days_between(released, utcnow()) or 0.0
        if age >= 30.0:
            return seq / age, "bootstrap"
    return None, None


def dwell_from_intake(item: Item) -> tuple[float | None, str | None]:
    """Little's Law: mean days on the shelf is depth divided by arrival rate."""
    rate, basis = intake_rate(item)
    depth = item.listing_count_avg
    if depth is None:
        depth = float(item.listing_count or 0)
    if not rate or rate <= 0 or depth <= 0:
        return None, None
    days = depth / rate
    if not math.isfinite(days):
        return None, None
    return min(MAX_DWELL_DAYS, max(MIN_DWELL_DAYS, days)), basis


def record_intake(item: Item, variants: list[dict], observed_at: datetime) -> None:
    """Advance the running maximum of the per-product intake counter.

    The highest number *currently listed* is not monotonic - when the newest
    copy sells, it drops. What matters is the highest number ever issued, so
    this only ever ratchets upward. The observation time is recorded on every
    look, including ones where the counter stood still, because a rate needs
    the full elapsed window in its denominator, not just the moments it moved.
    """
    numbers = [n for v in variants if (n := sequence_of(v.get("code"))) is not None]
    if not numbers:
        return
    highest = max(numbers)

    if item.intake_first_seq is None:
        item.intake_first_seq = highest
        item.intake_first_at = observed_at

    if item.intake_last_seq is None or highest > item.intake_last_seq:
        item.intake_last_seq = highest
    elif highest < (item.intake_first_seq or 0):
        # Lower than where we started: the counter is not what we think it is.
        # Re-anchor rather than reporting a negative rate.
        log.debug(
            "Intake counter for %s went backwards (%s < %s), re-anchoring",
            item.code,
            highest,
            item.intake_first_seq,
        )
        item.intake_first_seq = highest
        item.intake_first_at = observed_at
    item.intake_last_at = observed_at


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReconcileResult:
    appeared: list[Listing] = field(default_factory=list)
    vanished: list[Listing] = field(default_factory=list)
    repriced: list[Listing] = field(default_factory=list)
    reappeared: list[Listing] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.appeared or self.vanished or self.repriced or self.reappeared)


def reconcile(
    db: Session,
    item: Item,
    variants: list[dict],
    observed_at: datetime | None = None,
    commit: bool = False,
    sold_out: bool = False,
) -> ReconcileResult:
    """Match one detail fetch's copies against what we already knew.

    Copies we have not seen before get a row, bracketed by the previous detail
    fetch so their start has an upper bound. Copies that were live and are now
    absent are closed out. Copies still present get their price checked, since
    used stock is marked down while it waits and that markdown is often the
    most useful thing on the page.

    A copy that comes back after vanishing - a cancelled order, a re-grade -
    starts a fresh spell on the same row rather than being counted as a brand
    new copy with zero days behind it.
    """
    observed_at = _as_aware(observed_at) or utcnow()
    result = ReconcileResult()

    if not variants and not sold_out:
        # An empty list on its own says nothing about which copies sold. It is
        # as easily a response we failed to read, and treating that as
        # "everything vanished" would invent sales across the catalogue.
        #
        # It means something only when the shop itself says there is nothing
        # to buy, and only the caller can see that - so the caller says so
        # rather than this guessing from an absence.
        return result

    seen: dict[str, dict] = {}
    for variant in variants:
        code = (variant.get("code") or "").strip()
        if code:
            seen[code] = variant

    # Looked up by (provider, code) rather than through item.listings, because
    # the unique constraint is global. If the shop ever re-keys a product, a
    # copy could arrive here already owned by a different item, and inserting a
    # second row for it would raise and take the whole poll down with it.
    existing = {
        row.code: row
        for row in db.execute(
            select(Listing).where(
                Listing.provider == item.provider, Listing.code.in_(list(seen))
            )
        ).scalars()
    }
    for row in item.listings:
        existing.setdefault(row.code, row)
    live_before = [row for row in item.listings if row.status == ListingStatus.live]

    for code, variant in seen.items():
        listing = existing.get(code)
        price = variant.get("price")
        if listing is None:
            listing = Listing(
                item=item,
                provider=item.provider,
                code=code,
                sequence=sequence_of(code),
                price=price,
                last_price=price,
                currency=item.currency or "JPY",
                condition=variant.get("condition"),
                item_grade=variant.get("item_grade"),
                box_grade=variant.get("box_grade"),
                condition_note=variant.get("note"),
                appeared_after=item.prev_detail_fetch_at,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                status=ListingStatus.live,
                observations=1,
            )
            db.add(listing)
            result.appeared.append(listing)
            # The product's own clock for used stock. Only moved forwards, so
            # a re-read of an older copy cannot make the product look fresh.
            if item.last_listing_at is None or observed_at > item.last_listing_at:
                item.last_listing_at = observed_at
            continue

        if listing.item_id is not None and listing.item_id != item.id:
            # The same copy under a different product. Follow the shop rather
            # than keeping a duplicate that can never be reconciled again.
            log.info(
                "Copy %s moved from item %s to %s", code, listing.item_id, item.id
            )
            listing.item = item
        if listing.status == ListingStatus.gone:
            # Back on the shelf. Its earlier spell is over and measured; this
            # is a new one, so the clock restarts rather than spanning the gap.
            listing.status = ListingStatus.live
            listing.outcome = None
            listing.vanished_before = None
            listing.appeared_after = item.prev_detail_fetch_at
            listing.first_seen_at = observed_at
            listing.observations = 1
            result.reappeared.append(listing)
        else:
            listing.observations = (listing.observations or 1) + 1

        listing.last_seen_at = observed_at
        if listing.item_grade is None and variant.get("item_grade"):
            listing.item_grade = variant.get("item_grade")
            listing.box_grade = variant.get("box_grade")
            listing.condition = variant.get("condition")
        # Only copies the shop actually answered about carry one; the rest
        # come back from other_items with no remarks field at all, and silence
        # there is not the same as knowing there is nothing to say. So a note
        # is written when there is one and never cleared by its absence.
        if variant.get("note"):
            listing.condition_note = variant["note"]
        if price is not None and price != listing.last_price:
            listing.last_price = price
            db.add(
                PricePoint(
                    item=item,
                    listing=listing,
                    recorded_at=observed_at,
                    price=price,
                    currency=listing.currency,
                    in_stock=True,
                    sale_status="Listed",
                    condition_grade=listing.condition,
                )
            )
            result.repriced.append(listing)

    missing = [row for row in live_before if row.code not in seen]
    if missing:
        # Every copy going at once is as easily a shop-side withdrawal as that
        # many simultaneous sales, so it is recorded as the weaker claim.
        wholesale = (
            len(missing) >= BATCH_WITHDRAWAL_SIZE and len(missing) == len(live_before)
        )
        outcome = ListingOutcome.withdrawn if wholesale else ListingOutcome.sold
        for listing in missing:
            _close(db, item, listing, observed_at, outcome)
            result.vanished.append(listing)

    record_intake(item, variants, observed_at)
    item.listing_count = len(seen)
    previous = item.listing_count_avg
    item.listing_count_avg = (
        float(len(seen)) if previous is None else previous * 0.7 + len(seen) * 0.3
    )

    if result.changed:
        refresh_estimates(db, item)

    if commit:
        db.commit()
    else:
        db.flush()
    return result


def _close(
    db: Session,
    item: Item,
    listing: Listing,
    observed_at: datetime,
    outcome: ListingOutcome,
) -> None:
    listing.status = ListingStatus.gone
    listing.vanished_before = observed_at
    listing.outcome = outcome
    db.add(
        PricePoint(
            item=item,
            listing=listing,
            recorded_at=observed_at,
            price=listing.last_price if listing.last_price is not None else listing.price,
            currency=listing.currency,
            in_stock=False,
            sale_status="Sold out" if outcome == ListingOutcome.sold else "Withdrawn",
            condition_grade=listing.condition,
        )
    )


def close_all(
    db: Session,
    item: Item,
    observed_at: datetime | None = None,
    commit: bool = False,
) -> int:
    """The whole product went away, so every copy under it went with it.

    Called when AmiAmi answers a detail request with "no such item", which on
    a pre-owned product means the last copy sold and the listing was removed.
    """
    observed_at = _as_aware(observed_at) or utcnow()
    closed = 0
    for listing in item.listings:
        if listing.status == ListingStatus.live:
            _close(db, item, listing, observed_at, ListingOutcome.delisted)
            closed += 1
    if closed:
        item.listing_count = 0
        refresh_estimates(db, item)
        if commit:
            db.commit()
    return closed


# ---------------------------------------------------------------------------
# Product level, from data we already had
# ---------------------------------------------------------------------------


def product_sold_out_days(db: Session, item: Item) -> float | None:
    """How long the whole product stayed listed before it sold out.

    Coarser than following copies, but it needs nothing that was not already
    being recorded, so it can speak for products nobody has ever opened. Like
    every figure here it is a floor, not a measurement: the product was listed
    before we first saw it.
    """
    if not item.order_closed or item.in_stock:
        return None
    closed_at = db.execute(
        select(PricePoint.recorded_at)
        .where(
            PricePoint.item_id == item.id,
            PricePoint.listing_id.is_(None),
            PricePoint.in_stock.is_(False),
        )
        .order_by(PricePoint.recorded_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if closed_at is None:
        return None
    return _days_between(item.first_seen_at, closed_at)


# ---------------------------------------------------------------------------
# Cached estimates
# ---------------------------------------------------------------------------


def refresh_estimates(db: Session, item: Item) -> None:
    """Recompute the denormalized figures search and badges read.

    Preference order is precision first: copies we actually watched sell beat
    a rate derived from the counter, which beats the whole-product figure.
    """
    completed = [
        row
        for row in item.listings
        if row.status == ListingStatus.gone and row.outcome != ListingOutcome.withdrawn
    ]
    samples = _samples_for(item.listings)
    anchored = sum(1 for _, done in samples if done)

    item.dwell_samples = len(completed)
    if anchored >= MIN_OBSERVED_SAMPLES:
        median = kaplan_meier_median(samples)
        if median is not None:
            item.dwell_days = min(MAX_DWELL_DAYS, max(MIN_DWELL_DAYS, median))
            item.dwell_basis = "observed"
            return

    days, basis = dwell_from_intake(item)
    if days is not None:
        item.dwell_days = days
        item.dwell_basis = "intake" if basis == "measured" else "intake_bootstrap"
        return

    if item.sold_out_days:
        item.dwell_days = min(MAX_DWELL_DAYS, max(MIN_DWELL_DAYS, item.sold_out_days))
        item.dwell_basis = "product"


# ---------------------------------------------------------------------------
# Summary for the UI
# ---------------------------------------------------------------------------


#: Best first, matching how AmiAmi ranks its own condition notes.
_GRADE_ORDER = ["S", "A+", "A", "A-", "B+", "B", "B-", "C", "D"]


def _grade_key(grade: str | None) -> int:
    try:
        return _GRADE_ORDER.index((grade or "").upper())
    except ValueError:
        return len(_GRADE_ORDER)


def _cheapest_first_rate(listings: list[Listing]) -> tuple[int, int] | None:
    """How often the cheapest copy on the shelf was the one that sold.

    Worth knowing before deciding to wait: if the bargain always goes first,
    hesitating costs you the bargain rather than getting you a better one.
    """
    sold = [
        row
        for row in listings
        if row.status == ListingStatus.gone
        and row.outcome == ListingOutcome.sold
        and row.price is not None
    ]
    if not sold:
        return None

    wins = 0
    for row in sold:
        moment = _as_aware(row.last_seen_at)
        rivals = [
            other.last_price if other.last_price is not None else other.price
            for other in listings
            if other.id != row.id
            and other.price is not None
            and (_as_aware(other.first_seen_at) or moment) <= moment
            and (
                other.vanished_before is None
                or (_as_aware(other.vanished_before) or moment) > moment
            )
        ]
        mine = row.last_price if row.last_price is not None else row.price
        if not rivals or mine <= min(rivals):
            wins += 1
    return wins, len(sold)


def summary(db: Session, item: Item, now: datetime | None = None) -> dict:
    """Everything the item page needs to talk about shelf life honestly."""
    now = _as_aware(now) or utcnow()
    listings = sorted(
        item.listings,
        key=lambda row: (_as_aware(row.first_seen_at) or now),
        reverse=True,
    )

    # One query for every copy rather than one per row. A price point with a
    # listing attached is that copy being repriced - AmiAmi marks a used copy
    # down while it sits, sometimes by a couple of hundred yen at a time - and
    # those are written only when the figure actually moved, so there are a
    # handful per copy at most.
    trails: dict[int, list[dict]] = {}
    if listings:
        points = db.execute(
            select(PricePoint)
            .where(PricePoint.listing_id.in_([row.id for row in listings]))
            .order_by(PricePoint.recorded_at)
        ).scalars()
        for point in points:
            if point.price is None:
                continue
            trails.setdefault(point.listing_id, []).append(
                {
                    "at": point.recorded_at,
                    "price": point.price,
                    "in_stock": point.in_stock,
                }
            )

    rows = []
    for listing in listings:
        life = lifetime_of(listing, now)
        # The copy's own asking price over time, first seen first. The opening
        # price is not always among the points - one is only written when
        # something changed - so it is put at the front when it is missing,
        # otherwise a copy that was marked down once appears to have had only
        # the lower price all along.
        trail = trails.get(listing.id, [])
        opening = listing.price
        if opening is not None and (not trail or trail[0]["price"] != opening):
            trail = [
                {"at": listing.first_seen_at, "price": opening, "in_stock": True}
            ] + trail
        rows.append(
            {
                "code": listing.code,
                "sequence": listing.sequence,
                "price": listing.price,
                "last_price": listing.last_price,
                "currency": listing.currency,
                "condition": listing.condition,
                "condition_note": listing.condition_note,
                "item_grade": listing.item_grade,
                "box_grade": listing.box_grade,
                "status": listing.status.value,
                "outcome": listing.outcome.value if listing.outcome else None,
                "first_seen_at": listing.first_seen_at,
                "last_seen_at": listing.last_seen_at,
                "vanished_before": listing.vanished_before,
                "lifetime": life.as_dict(),
                "price_trail": trail,
            }
        )

    samples = _samples_for(listings, now)
    # Copies we watched leave. Not the same as usable samples: one that was
    # already on the shelf when we first looked did sell, but its true length
    # is unknown, so it informs the curve without anchoring it.
    departed = sum(
        1
        for row in listings
        if row.status == ListingStatus.gone and row.outcome != ListingOutcome.withdrawn
    )
    anchored = sum(1 for _, done in samples if done)
    median = kaplan_meier_median(samples) if anchored >= MIN_OBSERVED_SAMPLES else None

    by_grade = []
    grades: dict[str, list[tuple[float, bool]]] = {}
    for listing in listings:
        if not listing.item_grade:
            continue
        life = lifetime_of(listing, now)
        grades.setdefault(listing.item_grade.upper(), []).append(
            (life.certain_days, life.is_complete and not life.open_start)
        )
    for grade in sorted(grades, key=_grade_key):
        entries = grades[grade]
        done = sum(1 for _, ok in entries if ok)
        if done < 2:
            continue
        value = kaplan_meier_median(entries)
        if value is not None:
            by_grade.append({"grade": grade, "median_days": round(value, 1), "samples": done})

    rate, rate_basis = intake_rate(item)
    intake_days, _ = dwell_from_intake(item)
    cheapest = _cheapest_first_rate(listings)

    return {
        "listings": rows,
        "live_count": sum(1 for row in listings if row.status == ListingStatus.live),
        "observed_count": len(listings),
        "departed_count": departed,
        "anchored_count": anchored,
        "median_days": round(median, 1) if median is not None else None,
        "by_grade": by_grade,
        "intake_per_month": round(rate * 30.0, 1) if rate else None,
        "intake_basis": rate_basis,
        "intake_total": item.intake_last_seq,
        "intake_dwell_days": round(intake_days, 1) if intake_days else None,
        "dwell_days": round(item.dwell_days, 1) if item.dwell_days else None,
        "dwell_basis": item.dwell_basis,
        "sold_out_days": round(item.sold_out_days, 1) if item.sold_out_days else None,
        "cheapest_first": (
            {"wins": cheapest[0], "of": cheapest[1]} if cheapest else None
        ),
        "tracked_since": min(
            (_as_aware(row.first_seen_at) for row in listings),
            default=None,
        ),
    }


def daily_recap(db: Session, days: int = 14) -> dict:
    """Pre-owned copies arriving and leaving, day by day.

    Counted over individual copies rather than products, because that is what
    the shop actually moves: one product can take in five copies in a morning
    and sell three of them by evening without ever changing whether it is
    "listed". A product-level count would record none of that.

    Departures are split by what we can honestly claim. A copy that vanished
    while its product stayed on sale was sold; a whole product disappearing at
    once is as easily a withdrawal by the shop, and is recorded as the weaker
    claim - see reconcile().

    Days with nothing at all are still returned, so a quiet day reads as a
    quiet day rather than as a gap in the record.
    """
    now = utcnow()
    since = (now - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    def day_of(moment: datetime | None) -> str | None:
        if moment is None:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.date().isoformat()

    buckets: dict[str, dict] = {}
    for offset in range(days):
        key = (since + timedelta(days=offset)).date().isoformat()
        buckets[key] = {
            "date": key,
            "arrived": 0,
            "sold": 0,
            "withdrawn": 0,
            "gone": 0,
            "net": 0,
        }

    arrivals = db.execute(
        select(Listing.first_seen_at).where(Listing.first_seen_at >= since)
    ).scalars()
    for moment in arrivals:
        key = day_of(moment)
        if key in buckets:
            buckets[key]["arrived"] += 1

    departures = db.execute(
        select(Listing.vanished_before, Listing.outcome).where(
            Listing.vanished_before.is_not(None), Listing.vanished_before >= since
        )
    ).all()
    for moment, outcome in departures:
        key = day_of(moment)
        if key not in buckets:
            continue
        buckets[key]["gone"] += 1
        if outcome == ListingOutcome.sold:
            buckets[key]["sold"] += 1
        else:
            buckets[key]["withdrawn"] += 1

    rows = []
    for key in sorted(buckets, reverse=True):
        entry = buckets[key]
        entry["net"] = entry["arrived"] - entry["gone"]
        rows.append(entry)

    live = int(
        db.execute(
            select(func.count(Listing.id)).where(Listing.status == ListingStatus.live)
        ).scalar_one()
    )
    # Yesterday rather than today: today is still happening, and comparing a
    # part-day against whole ones makes every morning look like a collapse.
    complete = [r for r in rows if r["date"] != now.date().isoformat()]
    typical_in = (
        round(sum(r["arrived"] for r in complete) / len(complete), 1) if complete else 0.0
    )
    typical_out = (
        round(sum(r["gone"] for r in complete) / len(complete), 1) if complete else 0.0
    )

    return {
        "days": rows,
        "live_listings": live,
        "typical_arrivals": typical_in,
        "typical_departures": typical_out,
        "tracking_since": db.execute(
            select(func.min(Listing.first_seen_at))
        ).scalar_one_or_none(),
    }
