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
    AppSetting,
    CollectionEntry,
    Condition,
    Item,
    Listing,
    ListingStatus,
    Watch,
    utcnow,
)
from ..providers import ItemNotFound, ProviderError, get_provider
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

    return {product_code_of(code) for code in codes if code}


def tier_for(db: Session, item: Item, watched: set[str] | None = None) -> str:
    """How closely this product deserves to be followed."""
    watched = _watched_codes(db) if watched is None else watched

    if (item.code or "") in watched:
        return HOT

    on_a_list = db.execute(
        select(CollectionEntry.id).where(CollectionEntry.item_id == item.id).limit(1)
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


#: How often the sampler is allowed to walk the whole catalogue rather than
#: only what the tiers say is due. Once a day: the point is that a product
#: nothing has asked about in weeks still gets looked at occasionally, not
#: that everything is re-read constantly.
SWEEP_EVERY_HOURS = 24


#: Where the last catalogue walk is remembered. A row in the settings table
#: rather than a column: it is one timestamp for the whole job, not a fact
#: about any product.
SWEEP_MARKER = "shelfwatch:last_sweep"


def _last_sweep_at(db: Session):
    row = db.get(AppSetting, SWEEP_MARKER)
    stamp = (row.value or {}).get("at") if row else None
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:  # pragma: no cover - a hand-edited row
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _note_sweep(db: Session) -> None:
    row = db.get(AppSetting, SWEEP_MARKER)
    if row is None:
        row = AppSetting(key=SWEEP_MARKER)
        db.add(row)
    row.value = {"at": datetime.now(timezone.utc).isoformat()}


def sweep_candidates(db: Session, provider: str, limit: int, before) -> list[Item]:
    """Products nothing has looked at for longest, due or not.

    What this is for: the tiers keep re-reading whatever is hot, and a product
    that went quiet drops to the cold tier and then waits three days between
    looks - while a busy one is read for the twentieth time. When there is
    budget going spare, spending it on the least recently seen is worth more
    than another look at something already well covered.

    Ordered by when each was last examined, oldest first, so a walk like this
    works its way through the catalogue rather than picking the same handful.
    """
    return list(
        db.execute(
            select(Item)
            .where(
                Item.provider == provider,
                Item.condition == Condition.preowned,
                Item.order_closed.is_(False),
                or_(
                    Item.last_detail_fetch_at.is_(None),
                    Item.last_detail_fetch_at < before,
                ),
            )
            .order_by(Item.last_detail_fetch_at.asc().nulls_first(), Item.id)
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

    from .crawler import watches_are_due

    provider = get_provider(provider_id)
    pacer = _pacer()
    deadline = time.monotonic() + (budget_seconds or settings.shelf_max_seconds_per_run)
    started = time.monotonic()

    # Enough candidates to fill the budget even if every fetch is quick.
    headroom = int(settings.shelf_requests_per_minute * 4) + 10
    candidates = due_items(db, provider_id, headroom)

    # Nothing due, and the budget is going spare. Rather than idling, work
    # through the catalogue from the least recently examined - the tiers will
    # otherwise re-read a hot product for the twentieth time before a quiet
    # one is looked at once. Held to once a day, so this is a sweep and not a
    # second polling loop.
    swept = False
    if len(candidates) < headroom // 4:
        last = _last_sweep_at(db)
        if last is None or (
            datetime.now(timezone.utc) - last
        ) >= timedelta(hours=SWEEP_EVERY_HOURS):
            already = {item.id for item in candidates}
            extra = [
                item
                for item in sweep_candidates(
                    db,
                    provider_id,
                    headroom - len(candidates),
                    datetime.now(timezone.utc) - timedelta(hours=SWEEP_EVERY_HOURS),
                )
                if item.id not in already
            ]
            if extra:
                candidates = candidates + extra
                swept = True
                _note_sweep(db)

    reqlog.doing(
        "shelf",
        f"{len(candidates)} product(s) to re-read"
        + (" (filling spare budget from the least recently seen)" if swept else ""),
        due=len(candidates),
        catalogue_sweep=swept,
        budget_seconds=budget_seconds or settings.shelf_max_seconds_per_run,
    )
    if not candidates:
        run.stopped_because = "nothing due"
        return run

    watched = _watched_codes(db)
    from ..scheduler.engine import engine

    first = True
    for item in candidates:
        if getattr(engine, "stopping", False):
            run.stopped_because = "shutting down"
            break
        if time.monotonic() >= deadline:
            run.stopped_because = "time budget reached"
            break
        if watches_are_due(db):
            # Alerts are the point of the application; this is groundwork.
            run.stopped_because = "yielded to a due watch"
            break

        if not first:
            pacer.sleep()
        first = False

        tier = tier_for(db, item, watched)
        item.shelf_tier = tier
        item.shelf_due_at = utcnow() + _interval(tier)

        try:
            detailed = provider.get_item(item.code)
        except ItemNotFound:
            # The product is gone, which on a pre-owned listing means the last
            # copy sold. That is information, not a failure.
            catalog.mark_unavailable(db, item, commit=False)
            run.delisted += 1
            run.checked += 1
            db.commit()
            continue
        except ProviderError as exc:
            run.errors += 1
            log.debug("Shelf check failed for %s: %s", item.code, exc)
            db.commit()
            if run.errors >= 5:
                run.stopped_because = "too many upstream errors"
                break
            continue

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
    total = int(
        db.execute(
            select(func.count(Item.id)).where(
                Item.provider == provider, Item.condition == Condition.preowned
            )
        ).scalar_one()
        or 0
    )
    seen_once = int(
        db.execute(
            select(func.count(Item.id)).where(
                Item.provider == provider,
                Item.condition == Condition.preowned,
                Item.intake_first_seq.is_not(None),
            )
        ).scalar_one()
        or 0
    )
    rated = int(
        db.execute(
            select(func.count(Item.id)).where(
                Item.provider == provider, Item.dwell_days.is_not(None)
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
    return {
        "enabled": settings.shelf_tracking_enabled,
        "preowned_total": total,
        "counter_seen": seen_once,
        "with_estimate": rated,
        "due_now": due,
        "tiers": tiers,
        "by_basis": by_basis,
        "listings_total": listings_total,
        "listings_live": listings_live,
        "listings_departed": listings_total - listings_live,
        "requests_per_minute": settings.shelf_requests_per_minute,
    }
