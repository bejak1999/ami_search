"""Spending detail fetches where the shelf-life answer needs to be sharp.

Following individual copies costs one request per product per look, and the
catalogue is far too big to look at all of it often. So resolution follows
attention, the same way watch polling already does:

  hot   something is waiting on this product - a watch, a wishlist entry, or
        a price range that just moved on a cheap list sweep. Every couple of
        hours, so a sale is bracketed to within half a day.
  warm  a series or character the household collects, or a product already
        showing measurable turnover. Twice a day.
  cold  everything else. Every few days, purely to feed the statistics; for
        these the intake counter carries the estimate anyway, and that needs
        two looks a fortnight apart rather than two a day.

The whole schedule rides on one column. :attr:`Item.shelf_due_at` says when a
product is next owed a look, and anything that learns something interesting -
a moved price range, a new listing, a fired watch - simply pulls that date
forward. There is no separate queue to keep in step with reality.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from . import budget, reqlog
from ..models import (
    CollectionEntry,
    Condition,
    Item,
    Listing,
    ListingStatus,
    Watch,
    utcnow,
)
from ..providers import ItemNotFound, ProviderError, get_provider
from .catalog import counterpart_code
from . import catalog
from .pacing import HumanPacer

log = logging.getLogger(__name__)

HOT, WARM, COLD = "hot", "warm", "cold"


@dataclass
class ShelfRun:
    """What one pass of the sampler actually did."""

    checked: int = 0
    appeared: int = 0
    vanished: int = 0
    repriced: int = 0
    delisted: int = 0
    errors: int = 0
    took_ms: int = 0
    #: How often it stood aside for a watch and picked up again.
    interruptions: int = 0
    stopped_because: str = ""

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "appeared": self.appeared,
            "vanished": self.vanished,
            "repriced": self.repriced,
            "delisted": self.delisted,
            "errors": self.errors,
            "took_ms": self.took_ms,
            "stopped_because": self.stopped_because,
        }


def _pacer() -> HumanPacer:
    # Same shaping as the catalogue crawler: log-normal gaps, the occasional
    # long break, slower overnight. A steady metronome is what gets noticed.
    return HumanPacer(
        # The job this was written for: ten a minute for four minutes in
        # every ten came out at 3.4 a minute measured, while thirty-odd of
        # the allowance sat unused. It now takes whatever is going.
        rate_source=lambda: budget.rate_for("shelf"),
        sigma=settings.crawler_jitter_sigma,
        break_probability=settings.crawler_break_probability,
        quiet_hours=(settings.crawler_quiet_hours_start, settings.crawler_quiet_hours_end),
        quiet_slowdown=settings.crawler_quiet_slowdown,
    )


def _interval(tier: str) -> timedelta:
    hours = {
        HOT: settings.shelf_hot_interval_hours,
        WARM: settings.shelf_warm_interval_hours,
    }.get(tier, settings.shelf_cold_interval_hours)
    return timedelta(hours=max(0.25, float(hours)))


def _watched_codes(db: Session) -> set[str]:
    """Product codes someone is actively waiting on."""
    codes = set(
        db.execute(
            select(Watch.item_code).where(
                Watch.enabled.is_(True), Watch.item_code.is_not(None)
            )
        )
        .scalars()
        .all()
    )
    # A watch may name one copy, FIGURE-x-R027, but what it is really waiting
    # on is that product's shelf. Reduce here rather than at the item, since
    # the item already carries the product code and cannot be expanded back.
    from .shelflife import product_code_of

    products = {product_code_of(code) for code in codes if code}
    # And both listings of the figure. Somebody watching the new listing of a
    # figure with no used copies yet is waiting for precisely the copy this
    # job follows; leaving that product cold means checking the shelf they
    # care about every three days instead of every two hours.
    return products | {counterpart_code(code) for code in products}


def tier_for(db: Session, item: Item, watched: set[str] | None = None) -> str:
    """How closely this product deserves to be followed."""
    watched = _watched_codes(db) if watched is None else watched

    if (item.code or "") in watched:
        return HOT

    # Either listing of the figure counts. Somebody who saved the new listing
    # of a figure that has no used copies yet is waiting for exactly the copy
    # this job would be following, and leaving it cold is how the shelf they
    # care about is the one checked least often.
    on_a_list = db.execute(
        select(CollectionEntry.id)
        .join(Item, Item.id == CollectionEntry.item_id)
        .where(
            Item.provider == item.provider,
            Item.code.in_([item.code, counterpart_code(item.code)]),
        )
        .limit(1)
    ).first()
    if on_a_list:
        return HOT

    if item.dwell_samples or item.listing_count > 1:
        # It has demonstrated turnover, so looking again will learn something.
        return WARM

    if item.series and _collected_series(db).intersection({item.series}):
        return WARM

    return COLD


_series_cache: tuple[float, set[str]] = (0.0, set())


def _collected_series(db: Session) -> set[str]:
    """Series that appear in anyone's collection, cached for a few minutes.

    Recomputing this per item would run one query per candidate, which is the
    kind of thing that quietly turns a cheap job into an expensive one.
    """
    global _series_cache
    age, cached = _series_cache
    if time.monotonic() - age < 300:
        return cached
    rows = (
        db.execute(
            select(Item.series)
            .join(CollectionEntry, CollectionEntry.item_id == Item.id)
            .where(Item.series.is_not(None))
            .distinct()
        )
        .scalars()
        .all()
    )
    _series_cache = (time.monotonic(), set(rows))
    return _series_cache[1]


def promote(db: Session, item: Item, commit: bool = False) -> None:
    """Ask for a detail fetch as soon as the sampler next runs.

    Called when something cheap has hinted that the shelf moved - most often
    a price range that changed during a catalogue sweep.
    """
    item.shelf_due_at = utcnow()
    if commit:
        db.commit()


#: How much of each run is kept for products already examined at least once.
#: A first look can only add copies; only a second one can see a copy go. At
#: a quarter, discovery still leads while departures are actually observed -
#: raise it to see sales sooner, lower it to finish the first pass sooner.
REVISIT_SHARE = 0.25

#: How long a product waits after a failed look before it is offered again.
#: Short, because the failure says nothing about the product - only about the
#: moment - but not zero, or the run spends itself retrying the same row.
RETRY_AFTER_ERROR = timedelta(minutes=5)

#: Failures in a row before a run gives up. In a row rather than in total:
#: an upstream that refuses one request in ten is not an upstream that has
#: stopped answering, and treating it as one throws away the other nine.
ERRORS_BEFORE_GIVING_UP = 5


def revisit_candidates(db: Session, provider: str, limit: int) -> list[Item]:
    """Products we have seen before, longest ago first.

    Deliberately excludes anything never examined, which is the whole point.
    Both other queues put never-examined products first - ``due_items``
    because a first look unlocks the intake rate, and this one because it
    used to sort nulls first as well - so between them nothing ever got a
    *second* look while there was a backlog of first ones.

    That matters because a second look is the only thing that can see a copy
    leave. A first look can only ever add copies. With every request going to
    first looks, arrivals climbed and departures stayed near zero, which is
    not a fact about the shop but about the order of a query.
    """
    return list(
        db.execute(
            select(Item)
            .where(
                Item.provider == provider,
                Item.condition == Condition.preowned,
                Item.order_closed.is_(False),
                Item.last_detail_fetch_at.is_not(None),
            )
            .order_by(Item.last_detail_fetch_at.asc(), Item.id)
            .limit(limit)
        )
        .scalars()
        .all()
    )


def due_items(db: Session, provider: str, limit: int) -> list[Item]:
    """Products owed a look, most overdue first.

    Only pre-owned products qualify: a new-condition listing is one item at
    one price, so there are no copies to follow and nothing to learn.
    """
    now = datetime.now(timezone.utc)
    return list(
        db.execute(
            select(Item)
            .where(
                Item.provider == provider,
                Item.condition == Condition.preowned,
                Item.order_closed.is_(False),
                or_(Item.shelf_due_at.is_(None), Item.shelf_due_at <= now),
            )
            # Never looked at beats long overdue: the second observation of a
            # product is what unlocks its intake rate, and that one look buys
            # an estimate the cold tier would otherwise never get.
            .order_by(Item.shelf_due_at.is_(None).desc(), Item.shelf_due_at.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )


def run_once(
    db: Session,
    provider_id: str = "amiami",
    budget_seconds: int | None = None,
) -> ShelfRun:
    """Look at as many due products as the time budget allows."""
    run = ShelfRun()
    if not settings.shelf_tracking_enabled:
        run.stopped_because = "disabled"
        return run

    from .crawler import _wait_for_watches, watches_are_due

    provider = get_provider(provider_id)
    pacer = _pacer()
    deadline = time.monotonic() + (budget_seconds or settings.shelf_max_seconds_per_run)
    started = time.monotonic()

    # Enough candidates to fill the budget even if every fetch is quick.
    #
    # Sized from the pool this job draws on, not from the old per-job setting.
    # That setting still said ten a minute, so a run fetched fifty candidates
    # and then sat there having used half its four minutes - the sampler was
    # not slow, it had run out of things to look at.
    seconds = budget_seconds or settings.shelf_max_seconds_per_run
    headroom = int(budget.total_per_minute() * (seconds / 60.0)) + 10

    # Part of every run is kept for products we have already seen.
    #
    # This used to be a once-a-day top-up that only ran when fewer than a
    # quarter of the wanted candidates were due. On a catalogue this size
    # that condition is never met - there are always more than a handful of
    # overdue products - so the branch never executed once. And when it did
    # run it drew from a query that also sorted never-examined first, so it
    # would not have produced a single revisit anyway.
    #
    # A reserved share fixes both. It is a share rather than a schedule
    # because a second look is the only thing that can observe a copy
    # leaving: without one, this job can only ever count arrivals.
    reserved = int(headroom * REVISIT_SHARE)
    candidates = due_items(db, provider_id, headroom - reserved)

    already = {item.id for item in candidates}
    revisits = [
        item
        for item in revisit_candidates(db, provider_id, reserved + len(already))
        if item.id not in already
    ][:reserved]
    if revisits:
        candidates = candidates + revisits

    reqlog.doing(
        "shelf",
        f"{len(candidates)} product(s) to read"
        + (f", {len(revisits)} of them second looks" if revisits else ""),
        due=len(candidates),
        revisits=len(revisits),
        budget_seconds=budget_seconds or settings.shelf_max_seconds_per_run,
    )
    if not candidates:
        run.stopped_because = "nothing due"
        return run

    watched = _watched_codes(db)
    from ..scheduler.engine import engine

    first = True
    consecutive_errors = 0
    for item in candidates:
        if getattr(engine, "stopping", False):
            run.stopped_because = "shutting down"
            break
        if time.monotonic() >= deadline:
            run.stopped_because = "time budget reached"
            break
        if watches_are_due(db):
            # Alerts are the point of the application; this is groundwork, so
            # the watch goes first. But ending the run here forfeited whatever
            # was left of four minutes, and watches poll every minute or two -
            # so nearly every run died in its first seconds. Stand aside and
            # pick up again, exactly as the crawler does.
            run.interruptions += 1
            if not _wait_for_watches(db, deadline):
                run.stopped_because = "time budget reached"
                break
            continue

        if not first:
            pacer.sleep()
        first = False

        tier = tier_for(db, item, watched)
        item.shelf_tier = tier

        try:
            detailed = provider.get_item(item.code)
        except ItemNotFound:
            # The product is gone, which on a pre-owned listing means the last
            # copy sold. That is information, not a failure.
            catalog.mark_unavailable(db, item, commit=False)
            run.delisted += 1
            run.checked += 1
            consecutive_errors = 0  # the shop answered, and told us something
            db.commit()
            continue
        except ProviderError as exc:
            # A look that failed is not a look. Pushing the due date out
            # before the fetch meant one refused request retired a product
            # for the length of its whole interval - up to days for a cold
            # one - so a bad patch upstream quietly emptied the rotation of
            # everything it touched. It goes to the back of the queue for a
            # few minutes instead, which is long enough not to spin on it.
            item.shelf_due_at = utcnow() + RETRY_AFTER_ERROR
            consecutive_errors += 1
            run.errors += 1
            log.debug("Shelf check failed for %s: %s", item.code, exc)
            db.commit()
            # Consecutive, not cumulative. Counting every failure in the run
            # meant a steady low rate of refusals ended it early: at one in
            # twenty, a run stopped after a hundred fetches; at one in six it
            # stopped after thirty, and the job looked far slower than it was
            # for a reason that had nothing to do with its budget.
            if consecutive_errors >= ERRORS_BEFORE_GIVING_UP:
                run.stopped_because = "too many upstream errors in a row"
                break
            continue

        consecutive_errors = 0
        item.shelf_due_at = utcnow() + _interval(tier)

        before = {row.id for row in item.listings if row.status == ListingStatus.live}
        priced = {row.id: row.last_price for row in item.listings}
        catalog.upsert_item(db, detailed, commit=False)
        db.flush()
        after = {row.id for row in item.listings if row.status == ListingStatus.live}
        run.appeared += len(after - before)
        run.vanished += len(before - after)
        run.repriced += sum(
            1
            for row in item.listings
            if row.id in priced and row.last_price != priced[row.id]
        )
        run.checked += 1
        db.commit()

    run.took_ms = int((time.monotonic() - started) * 1000)
    if not run.stopped_because:
        run.stopped_because = "candidates exhausted"
    return run


# ---------------------------------------------------------------------------
# Coverage, for the admin panel
# ---------------------------------------------------------------------------


def coverage(db: Session, provider: str = "amiami") -> dict:
    """How much of the pre-owned catalogue is actually being followed."""
    # Products this job can actually work on. A sold-out one is never
    # selected - due_items excludes it - so counting it in the denominator
    # made the coverage bar unreachable by construction, and pushed it
    # further down every time something sold.
    total = int(
        db.execute(
            select(func.count(Item.id)).where(
                Item.provider == provider,
                Item.condition == Condition.preowned,
                Item.order_closed.is_(False),
            )
        ).scalar_one()
        or 0
    )
    closed = int(
        db.execute(
            select(func.count(Item.id)).where(
                Item.provider == provider,
                Item.condition == Condition.preowned,
                Item.order_closed.is_(True),
            )
        ).scalar_one()
        or 0
    )
    # Products this job has actually opened. Counted from the detail fetch,
    # not from whether the copies carried a readable intake number: about
    # fourteen hundred products had been opened and answered with copies this
    # could not number, and they were missing from a figure labelled "opened
    # at least once", which is not what it was measuring.
    seen_once = int(
        db.execute(
            select(func.count(Item.id)).where(
                Item.provider == provider,
                Item.condition == Condition.preowned,
                Item.order_closed.is_(False),
                Item.last_detail_fetch_at.is_not(None),
            )
        ).scalar_one()
        or 0
    )
    counter_anchored = int(
        db.execute(
            select(func.count(Item.id)).where(
                Item.provider == provider,
                Item.condition == Condition.preowned,
                Item.order_closed.is_(False),
                Item.intake_first_seq.is_not(None),
            )
        ).scalar_one()
        or 0
    )
    # Counted over the same population as the figure it is shown against,
    # or the two bars are drawn on different denominators and the shorter
    # one is not necessarily the smaller.
    rated = int(
        db.execute(
            select(func.count(Item.id)).where(
                Item.provider == provider,
                Item.condition == Condition.preowned,
                Item.order_closed.is_(False),
                Item.dwell_days.is_not(None),
            )
        ).scalar_one()
        or 0
    )
    tiers = {
        tier: int(count or 0)
        for tier, count in db.execute(
            select(Item.shelf_tier, func.count(Item.id))
            .where(Item.provider == provider, Item.shelf_tier.is_not(None))
            .group_by(Item.shelf_tier)
        ).all()
    }
    by_basis = {
        basis: int(count or 0)
        for basis, count in db.execute(
            select(Item.dwell_basis, func.count(Item.id))
            .where(Item.dwell_basis.is_not(None))
            .group_by(Item.dwell_basis)
        ).all()
    }
    listings_total = int(db.execute(select(func.count(Listing.id))).scalar_one() or 0)
    listings_live = int(
        db.execute(
            select(func.count(Listing.id)).where(Listing.status == ListingStatus.live)
        ).scalar_one()
        or 0
    )
    # Split by what we can honestly claim. "Departed" covered every copy no
    # longer on the shelf, and was labelled as having been seen to sell -
    # which swept in the batch disappearances we deliberately record as the
    # weaker claim, and the ones a repair closed without watching them go.
    by_outcome = {
        (outcome.value if outcome else "unknown"): int(count or 0)
        for outcome, count in db.execute(
            select(Listing.outcome, func.count(Listing.id))
            .where(Listing.status == ListingStatus.gone)
            .group_by(Listing.outcome)
        ).all()
    }
    sold = by_outcome.get("sold", 0) + by_outcome.get("delisted", 0)
    now = datetime.now(timezone.utc)
    due = int(
        db.execute(
            select(func.count(Item.id)).where(
                Item.provider == provider,
                Item.condition == Condition.preowned,
                Item.order_closed.is_(False),
                or_(Item.shelf_due_at.is_(None), Item.shelf_due_at <= now),
            )
        ).scalar_one()
        or 0
    )
    # How each tier is doing against the cadence it was promised, rather than
    # against a daily round nobody designed. Hot is looked at every couple of
    # hours because somebody is waiting on it; cold every three days because
    # it only has to feed the statistics. A single "seen today" bar would
    # report the cold tier as permanently behind on a schedule it does not
    # have.
    cadence = []
    demanded_per_hour = 0.0
    for tier, hours in (
        (HOT, settings.shelf_hot_interval_hours),
        (WARM, settings.shelf_warm_interval_hours),
        (COLD, settings.shelf_cold_interval_hours),
    ):
        window = max(0.25, float(hours))
        in_tier = int(
            db.execute(
                select(func.count(Item.id)).where(
                    Item.provider == provider,
                    Item.condition == Condition.preowned,
                    Item.order_closed.is_(False),
                    Item.shelf_tier == tier,
                )
            ).scalar_one()
            or 0
        )
        fresh = int(
            db.execute(
                select(func.count(Item.id)).where(
                    Item.provider == provider,
                    Item.condition == Condition.preowned,
                    Item.order_closed.is_(False),
                    Item.shelf_tier == tier,
                    Item.last_detail_fetch_at >= now - timedelta(hours=window),
                )
            ).scalar_one()
            or 0
        )
        late = int(
            db.execute(
                select(func.count(Item.id)).where(
                    Item.provider == provider,
                    Item.condition == Condition.preowned,
                    Item.order_closed.is_(False),
                    Item.shelf_tier == tier,
                    or_(Item.shelf_due_at.is_(None), Item.shelf_due_at <= now),
                )
            ).scalar_one()
            or 0
        )
        oldest = db.execute(
            select(func.min(Item.last_detail_fetch_at)).where(
                Item.provider == provider,
                Item.condition == Condition.preowned,
                Item.order_closed.is_(False),
                Item.shelf_tier == tier,
                Item.last_detail_fetch_at.is_not(None),
            )
        ).scalar_one_or_none()
        oldest_hours = None
        if oldest is not None:
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            oldest_hours = round((now - oldest).total_seconds() / 3600, 1)
        demanded_per_hour += in_tier / window
        cadence.append(
            {
                "tier": tier,
                "products": in_tier,
                "every_hours": window,
                "overdue": late,
                "seen_in_window": fresh,
                "oldest_look_hours": oldest_hours,
                # A tier is keeping up when the product it has neglected
                # longest is still inside its own interval.
                "keeping_up": oldest_hours is not None and oldest_hours <= window,
            }
        )

    # What is actually being got through, measured the way every other
    # estimate here is: from the timestamps, not from the settings.
    looks_last_hour = int(
        db.execute(
            select(func.count(Item.id)).where(
                Item.provider == provider,
                Item.last_detail_fetch_at >= now - timedelta(hours=1),
            )
        ).scalar_one()
        or 0
    )
    looks_last_day = int(
        db.execute(
            select(func.count(Item.id)).where(
                Item.provider == provider,
                Item.last_detail_fetch_at >= now - timedelta(hours=24),
            )
        ).scalar_one()
        or 0
    )

    # How firm the figures are, ordered from measured to guessed. The panel
    # used to show two bars that differed by two hundred out of eleven
    # thousand, because almost every product that gets opened gets a figure
    # immediately - so the second bar re-measured the first.
    confidence = [
        {"basis": basis, "products": by_basis.get(basis, 0), "label": label}
        for basis, label in (
            ("observed", "Copies we watched sell"),
            ("intake", "Measured shop turnover"),
            ("intake_bootstrap", "Turnover, estimated"),
            ("product", "Whole listing only"),
        )
    ]

    # And what is still owed. A ladder, each rung excluding the ones below it,
    # so the four add up to the population exactly - a stacked bar whose parts
    # overlap is not a bar, it is four bars drawn on top of each other.
    FIRM_BASES = ("observed", "intake")

    def in_stage(*conditions) -> int:
        return int(
            db.execute(
                select(func.count(Item.id)).where(
                    Item.provider == provider,
                    Item.condition == Condition.preowned,
                    Item.order_closed.is_(False),
                    *conditions,
                )
            ).scalar_one()
            or 0
        )

    never = in_stage(Item.last_detail_fetch_at.is_(None))
    one_look = in_stage(
        Item.last_detail_fetch_at.is_not(None), Item.prev_detail_fetch_at.is_(None)
    )
    # Looked at more than once, so the figure has had a chance to firm up.
    looked_twice = (
        Item.last_detail_fetch_at.is_not(None),
        Item.prev_detail_fetch_at.is_not(None),
    )
    firm = in_stage(*looked_twice, Item.dwell_basis.in_(FIRM_BASES))
    estimated = in_stage(*looked_twice, Item.dwell_basis.not_in(FIRM_BASES))
    unrated = in_stage(*looked_twice, Item.dwell_basis.is_(None))

    progress = [
        {"stage": "never_opened", "products": never, "label": "Never opened"},
        {"stage": "one_look", "products": one_look,
         "label": "Opened once, awaiting a second look"},
        {"stage": "no_figure", "products": unrated,
         "label": "Looked at again, still no figure"},
        {"stage": "estimated", "products": estimated,
         "label": "Has a figure, but an estimated one"},
        {"stage": "firm", "products": firm, "label": "Resting on a measurement"},
    ]

    return {
        "enabled": settings.shelf_tracking_enabled,
        "preowned_total": total,
        "cadence": cadence,
        "demanded_per_hour": round(demanded_per_hour, 1),
        "looks_per_hour": looks_last_hour,
        "looks_last_day": looks_last_day,
        "confidence": confidence,
        "progress": progress,
        "counter_anchored": counter_anchored,
        #: Records kept of products the shop has stopped selling. Outside the
        #: denominator above, because nothing will ever look at them again.
        "preowned_closed": closed,
        "counter_seen": seen_once,
        "with_estimate": rated,
        "due_now": due,
        "tiers": tiers,
        "by_basis": by_basis,
        "listings_total": listings_total,
        "listings_live": listings_live,
        "listings_departed": listings_total - listings_live,
        "listings_sold": sold,
        "listings_by_outcome": by_outcome,
        # What it may actually use right now, not the per-job setting it no
        # longer reads. The panel was reporting ten a minute while the job
        # was drawing on a shared pool of twenty-four.
        "requests_per_minute": round(budget.rate_for("shelf"), 1),
        "budget_per_minute": budget.total_per_minute(),
    }
