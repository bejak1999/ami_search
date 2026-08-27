"""Background catalogue ingest.

Discovery needs a corpus. A catalogue assembled only from whatever the user
happened to search for is not one, which is why the discovery page felt empty:
it was filtering a handful of accidental results.

This walks the shop slice by slice, page by page, recording every item and its
price. It is deliberately unhurried, it yields the moment a watch is due, and
it remembers its cursor so a restart resumes rather than starting over.

Once the first pass is done the shop only adds a modest number of listings a
day, so later cycles just re-read the newest pages and a full sweep runs
occasionally to catch anything that changed further back.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import Integer, func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import CatalogCrawl, CrawlState, Item, Watch, utcnow
from ..providers import ProviderError, SearchQuery, get_provider
from . import catalog
from .pacing import HumanPacer

log = logging.getLogger(__name__)

#: The slices we build, best first. Pre-owned moves fastest and is what people
#: actually hunt, so it fills in before the much larger long tail.
#: How each slice's membership is expressed as a local query, so coverage can
#: be counted from the database instead of from a counter that only ever grows.
SCOPE_FILTERS: dict[str, str] = {
    "figures_preowned": "preowned",
    "figures_in_stock": "in_stock",
    "figures_preorder": "preorder",
    "figures_all": "all",
}

#: The slices overlap on purpose, and it is worth being clear about how.
#:
#: "All figures" is the only complete one - every other slice is a filtered
#: view of the same shop, so the same listing is genuinely reached twice. That
#: is not waste: the narrow slices are fast lanes that revisit the part of the
#: catalogue that changes, while the full sweep is what keeps the long tail
#: honest and eventually corrects a status nothing else has rechecked.
#:
#: One thing the head pages cannot do is catch changes. The shop's default
#: order is roughly "orderable first, by release date", not "recently
#: changed", so a price moving on a figure that came out last year happens
#: somewhere around page sixty and no head pass will ever see it. The head
#: pages are for finding listings that are new; the full sweep and the
#: shelf-life sampler are for noticing that something moved.
DEFAULT_SCOPES: list[dict] = [
    {
        "scope": "figures_preowned",
        "label": "Pre-owned figures",
        "priority": 10,
        "query": {"category_id": 1, "condition": "preowned"},
        "head_pages": 20,
        # The one slice that genuinely turns over hour to hour: a used copy
        # can be listed and sold inside a morning.
        "recheck_interval_minutes": 30,
    },
    {
        "scope": "figures_in_stock",
        "label": "Figures in stock",
        "priority": 20,
        "query": {"category_id": 1, "stock_filter": "in_stock"},
        "head_pages": 15,
        # First-hand stock does not appear and vanish the way used copies do,
        # and its head pages are the same listings the full sweep covers, so
        # twice a day is plenty and leaves the budget for work that pays.
        "recheck_interval_minutes": 720,
    },
    {
        "scope": "figures_preorder",
        "label": "Figures on pre-order",
        "priority": 30,
        "query": {"category_id": 1, "stock_filter": "preorder"},
        "head_pages": 10,
        # Pre-orders are announced, not restocked. A day's granularity loses
        # nothing except requests.
        "recheck_interval_minutes": 1440,
    },
    {
        "scope": "figures_all",
        "label": "All figures",
        "priority": 90,
        "query": {"category_id": 1},
        "head_pages": 30,
        "recheck_interval_minutes": 180,
    },
]


@dataclass
class CrawlRun:
    """What one scheduled slice of crawling actually did."""

    scope: str = ""
    pages: int = 0
    items: int = 0
    new_items: int = 0
    changed: int = 0
    seconds: float = 0.0
    stopped_because: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scope": self.scope,
            "pages": self.pages,
            "items": self.items,
            "new_items": self.new_items,
            "changed": self.changed,
            "seconds": round(self.seconds, 1),
            "stopped_because": self.stopped_because,
            "errors": self.errors,
        }


def ensure_scopes(db: Session, provider: str = "amiami") -> int:
    """Create the default slices the first time round."""
    created = 0
    for spec in DEFAULT_SCOPES:
        exists = db.execute(
            select(CatalogCrawl).where(
                CatalogCrawl.provider == provider, CatalogCrawl.scope == spec["scope"]
            )
        ).scalar_one_or_none()
        if exists is not None:
            continue
        db.add(
            CatalogCrawl(
                provider=provider,
                scope=spec["scope"],
                label=spec["label"],
                priority=spec["priority"],
                query=spec["query"],
                head_pages=spec["head_pages"],
                recheck_interval_minutes=spec.get("recheck_interval_minutes", 30),
            )
        )
        created += 1
    if created:
        db.commit()
        log.info("Registered %s catalogue slices for %s", created, provider)
    return created


def _pacer() -> HumanPacer:
    return HumanPacer(
        requests_per_minute=settings.crawler_requests_per_minute,
        sigma=settings.crawler_jitter_sigma,
        break_probability=settings.crawler_break_probability,
        quiet_hours=(settings.crawler_quiet_hours_start, settings.crawler_quiet_hours_end),
        quiet_slowdown=settings.crawler_quiet_slowdown,
    )


def watches_are_due(db: Session) -> bool:
    """True when a user's watch wants the request budget.

    Alerts are the point of the application; the catalogue build is
    housekeeping. Whenever the two compete, housekeeping stands aside.
    """
    now = datetime.now(timezone.utc)
    return (
        db.execute(
            select(Watch.id)
            .where(
                Watch.enabled.is_(True),
                or_(Watch.next_run_at.is_(None), Watch.next_run_at <= now),
            )
            .limit(1)
        ).first()
        is not None
    )


def _build_query(crawl: CatalogCrawl, page: int) -> SearchQuery:
    spec = dict(crawl.query or {})
    return SearchQuery(
        keywords=spec.get("keywords", ""),
        page=page,
        per_page=crawl.per_page,
        condition=spec.get("condition", "any"),
        stock_filter=spec.get("stock_filter", "any"),
        sort=spec.get("sort", "newest"),
        category_id=spec.get("category_id"),
        maker_id=spec.get("maker_id"),
        series_id=spec.get("series_id"),
    )


def _page_limit(crawl: CatalogCrawl) -> int:
    """How deep this cycle should go.

    The first pass reads everything. Later passes only re-read the newest
    pages, because the shop lists newest first, unless a full sweep is due.

    Column defaults only apply on insert, so every number is read defensively:
    a row that has not been flushed yet still carries None.
    """
    pages_total = crawl.pages_total or 0
    if not pages_total:
        return 10_000  # unknown until the first response comes back
    if not (crawl.cycles_completed or 0):
        return pages_total

    last = crawl.last_full_sweep_at
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        interval = max(1, crawl.full_sweep_interval_days or 7)
        if datetime.now(timezone.utc) - last >= timedelta(days=interval):
            return pages_total
    return min(pages_total, max(1, crawl.head_pages or 20))


def _cooldown_remaining(crawl: CatalogCrawl) -> int:
    """Seconds until this slice is allowed to start its next pass.

    Only applies between cycles. A pass already under way always continues, so
    the cooldown never leaves a slice stranded half-finished.
    """
    if (crawl.cursor_page or 1) > 1:
        return 0  # mid-pass
    if not (crawl.cycles_completed or 0):
        return 0  # never run
    finished = crawl.finished_at
    if finished is None:
        return 0
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    wait = timedelta(minutes=max(1, crawl.recheck_interval_minutes or 30))
    remaining = (finished + wait) - datetime.now(timezone.utc)
    return max(0, int(remaining.total_seconds()))


def _select_crawl(db: Session, provider: str) -> CatalogCrawl | None:
    candidates = list(
        db.execute(
            select(CatalogCrawl)
            .where(
                CatalogCrawl.provider == provider,
                CatalogCrawl.enabled.is_(True),
                CatalogCrawl.consecutive_errors < 5,
            )
            .order_by(
                # Anything mid-sweep finishes before a fresh slice starts, so
                # progress is visible rather than spread thinly everywhere.
                (CatalogCrawl.state == CrawlState.running).desc(),
                CatalogCrawl.priority.asc(),
                CatalogCrawl.last_run_at.asc().nulls_first(),
            )
        )
        .scalars()
        .all()
    )
    # Skip slices still resting between passes, so the budget goes to a slice
    # that has work to do instead of re-reading the same first pages.
    for crawl in candidates:
        if _cooldown_remaining(crawl) == 0:
            return crawl
    return None


def run_once(db: Session, provider_id: str = "amiami", budget_seconds: int | None = None) -> CrawlRun:
    """Crawl for a while, then stop and leave the cursor where it is."""
    run = CrawlRun()
    if not settings.crawler_enabled:
        run.stopped_because = "disabled"
        return run

    ensure_scopes(db, provider_id)
    crawl = _select_crawl(db, provider_id)
    if crawl is None:
        run.stopped_because = "no slice available"
        return run

    run.scope = crawl.scope
    provider = get_provider(provider_id)
    pacer = _pacer()
    deadline = time.monotonic() + (budget_seconds or settings.crawler_max_seconds_per_run)
    started = time.monotonic()

    if crawl.started_at is None:
        crawl.started_at = utcnow()
    crawl.state = CrawlState.running
    crawl.last_run_at = utcnow()
    db.commit()

    from ..scheduler.engine import engine

    first_request = True
    while True:
        if getattr(engine, "stopping", False):
            # A container update is in progress. The cursor is already
            # committed, so stopping here means the next start resumes at the
            # very next page rather than repeating this one.
            run.stopped_because = "shutting down"
            break
        if time.monotonic() >= deadline:
            run.stopped_because = "time budget reached"
            break
        if watches_are_due(db):
            run.stopped_because = "yielded to a due watch"
            break

        limit = _page_limit(crawl)
        if crawl.cursor_page > limit:
            _complete_cycle(db, crawl)
            run.stopped_because = "cycle complete"
            break

        # Space the requests out unevenly. The first one goes straight away so
        # a manual run in the admin view feels responsive.
        if not first_request:
            pacer.sleep()
        first_request = False

        try:
            result = provider.search(_build_query(crawl, crawl.cursor_page))
        except ProviderError as exc:
            crawl.consecutive_errors += 1
            crawl.last_error = str(exc)[:500]
            run.errors.append(str(exc))
            db.commit()
            run.stopped_because = "upstream error"
            break

        crawl.consecutive_errors = 0
        crawl.last_error = None
        if result.total:
            crawl.total_results = result.total
            crawl.pages_total = max(1, math.ceil(result.total / max(1, crawl.per_page)))

        if not result.items:
            # Ran off the end sooner than the reported total suggested.
            _complete_cycle(db, crawl)
            run.stopped_because = "no more results"
            break

        new_count, changed_count = _store_page(db, result.items)
        crawl.pages_fetched += 1
        crawl.items_seen += len(result.items)
        crawl.items_new += new_count
        crawl.items_changed += changed_count
        crawl.cursor_page += 1
        db.commit()

        run.pages += 1
        run.items += len(result.items)
        run.new_items += new_count
        run.changed += changed_count

    if crawl.state == CrawlState.running and run.stopped_because not in ("cycle complete",):
        crawl.state = CrawlState.paused
    db.commit()

    run.seconds = time.monotonic() - started
    if run.pages:
        log.info(
            "Crawled %s: %s pages, %s items (%s new) in %.0fs, stopped because %s",
            crawl.scope,
            run.pages,
            run.items,
            run.new_items,
            run.seconds,
            run.stopped_because,
        )
    return run


def _store_page(db: Session, items) -> tuple[int, int]:
    """Persist one page. Returns (new items, items whose price or stock moved)."""
    new_count = 0
    changed_count = 0
    for normalized in items:
        existed = catalog.get_item(db, normalized.provider, normalized.code) is not None
        _item, changed = catalog.upsert_item(db, normalized, commit=False)
        if not existed:
            new_count += 1
        elif changed:
            changed_count += 1
    db.commit()
    return new_count, changed_count


def _complete_cycle(db: Session, crawl: CatalogCrawl) -> None:
    """Wrap a finished pass and arm the next one."""
    was_full = (crawl.cursor_page or 1) > max(1, crawl.head_pages or 20) or not (
        crawl.cycles_completed or 0
    )
    crawl.cycles_completed = (crawl.cycles_completed or 0) + 1
    crawl.cursor_page = 1
    crawl.finished_at = utcnow()
    crawl.state = CrawlState.completed
    if was_full:
        crawl.last_full_sweep_at = utcnow()
    db.commit()
    log.info(
        "Finished a %s pass over %s (%s cycles done, %s items known)",
        "full" if was_full else "head",
        crawl.scope,
        crawl.cycles_completed,
        crawl.items_seen,
    )


def local_count(db: Session, scope: str, provider_id: str) -> int:
    """How many distinct items this slice has actually put in the database.

    Counting rows beats trusting a counter: ``items_seen`` accumulates fifty
    per page on every pass forever, so after a few dozen head passes it read
    "52,691 of ~10,475", which is not a ratio of anything.
    """
    from ..models import Condition

    stmt = select(func.count(Item.id)).where(Item.provider == provider_id)
    kind = SCOPE_FILTERS.get(scope, "all")
    if kind == "preowned":
        stmt = stmt.where(Item.condition == Condition.preowned)
    elif kind == "in_stock":
        stmt = stmt.where(Item.in_stock.is_(True))
    elif kind == "preorder":
        stmt = stmt.where(Item.is_preorder.is_(True))
    return int(db.execute(stmt).scalar_one() or 0)


def activity_profile(db: Session, provider_id: str = "amiami", days: int = 30) -> dict:
    """When the shop is actually busy, by hour of day and day of week.

    Built from timestamps already recorded rather than a new counter: an item
    row knows when it first appeared here, and a price point knows when
    something moved. Both are in UTC and are returned as UTC buckets, because
    only the browser knows what the reader's clock says.

    One honest caveat, which the view repeats: what this measures is when *we*
    noticed, so it is blurred by the polling interval and it cannot show
    activity during a stretch when nothing was polling at all. It is good
    enough to find the daily rhythm - Japanese business hours are a strong
    signal - and that is what it is for.
    """
    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))

    def buckets(column, expression: str, extra=None) -> dict[int, int]:
        stmt = select(
            func.cast(func.strftime(expression, column), Integer).label("bucket"),
            func.count().label("n"),
        ).where(column >= since)
        if extra is not None:
            stmt = stmt.where(extra)
        stmt = stmt.group_by("bucket")
        return {
            int(bucket): int(count)
            for bucket, count in db.execute(stmt).all()
            if bucket is not None
        }

    from ..models import PricePoint

    listings_hour = buckets(Item.first_seen_at, "%H", Item.provider == provider_id)
    listings_day = buckets(Item.first_seen_at, "%w", Item.provider == provider_id)
    changes_hour = buckets(PricePoint.recorded_at, "%H")
    changes_day = buckets(PricePoint.recorded_at, "%w")

    def series(data: dict[int, int], size: int) -> list[int]:
        return [data.get(index, 0) for index in range(size)]

    observed = int(
        db.execute(
            select(func.count(Item.id)).where(
                Item.provider == provider_id, Item.first_seen_at >= since
            )
        ).scalar_one()
        or 0
    )
    return {
        "days": days,
        "new_listings": observed,
        "price_changes": sum(changes_hour.values()),
        # Index 0 is midnight UTC; index 0 of the weekday series is Sunday,
        # matching strftime's %w.
        "listings_by_hour_utc": series(listings_hour, 24),
        "changes_by_hour_utc": series(changes_hour, 24),
        "listings_by_weekday": series(listings_day, 7),
        "changes_by_weekday": series(changes_day, 7),
    }


def progress(db: Session, provider_id: str = "amiami") -> dict:
    """Everything the admin view needs to show how the build is going."""
    crawls = list(
        db.execute(
            select(CatalogCrawl)
            .where(CatalogCrawl.provider == provider_id)
            .order_by(CatalogCrawl.priority)
        )
        .scalars()
        .all()
    )

    slices = []
    for crawl in crawls:
        limit = _page_limit(crawl) if crawl.pages_total else 0
        done = min((crawl.cursor_page or 1) - 1, limit) if limit else 0
        local = local_count(db, crawl.scope, provider_id)
        upstream = crawl.total_results or 0
        slices.append(
            {
                "scope": crawl.scope,
                "coverage_percent": (
                    round(min(local, upstream) / upstream * 100, 1) if upstream else 0.0
                ),
                "label": crawl.label or crawl.scope,
                "state": crawl.state.value,
                "enabled": crawl.enabled,
                "cursor_page": crawl.cursor_page,
                "pages_total": crawl.pages_total,
                "pages_this_cycle": limit,
                # Two different things, kept apart because conflating them is
                # what produced the nonsense ratio: how far through the
                # current pass we are, and how much of the slice we hold.
                "pass_percent": round(done / limit * 100, 1) if limit else 0.0,
                "total_results": crawl.total_results,
                # Distinct items this slice holds locally, counted now.
                "items_local": local,
                # Cumulative across every pass ever run, hence separate.
                "listings_checked": crawl.items_seen,
                "items_new": crawl.items_new,
                "items_changed": crawl.items_changed,
                "cycles_completed": crawl.cycles_completed,
                "first_pass_done": crawl.cycles_completed > 0,
                "last_run_at": crawl.last_run_at,
                "last_full_sweep_at": crawl.last_full_sweep_at,
                "last_error": crawl.last_error,
                "eta_seconds": _eta_seconds(limit - done),
                "next_run_in_seconds": _cooldown_remaining(crawl),
                "recheck_minutes": crawl.recheck_interval_minutes,
                "head_pages": crawl.head_pages,
                "full_sweep_interval_days": crawl.full_sweep_interval_days,
            }
        )

    known = int(db.execute(select(func.count(Item.id))).scalar_one() or 0)
    linked = int(
        db.execute(select(func.count(Item.id)).where(Item.mfc_id.is_not(None))).scalar_one() or 0
    )
    return {
        "enabled": settings.crawler_enabled,
        "requests_per_minute": settings.crawler_requests_per_minute,
        "run_interval_minutes": settings.crawler_run_interval_minutes,
        "seconds_per_run": settings.crawler_max_seconds_per_run,
        "slices": slices,
        "items_known": known,
        "items_linked_to_mfc": linked,
        "first_pass_complete": all(s["first_pass_done"] for s in slices) if slices else False,
    }


def _eta_seconds(pages_remaining: int) -> int | None:
    """Rough time to finish, using the configured rate and duty cycle."""
    if pages_remaining <= 0:
        return None
    seconds_per_page = 60.0 / max(0.1, settings.crawler_requests_per_minute)
    working = seconds_per_page * pages_remaining
    # Only part of each interval is spent crawling.
    duty = max(0.05, min(1.0, settings.crawler_max_seconds_per_run / (settings.crawler_run_interval_minutes * 60)))
    return int(working / duty)
