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
    # Left over from the version that split the head into its own slice. Kept
    # so an installation that ran it still counts that row against the right
    # part of the catalogue while the migration removes it.
    "figures_preowned_head": "preowned",
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
#: The four slices are the shop's own statuses, and the overlap between them
#: was measured rather than assumed:
#:
#:   All figures     69,176   every listing, used ones included
#:   Pre-owned       10,481   a subset, and the only one that isolates used
#:   In stock         2,906   a subset, with no used listings in it at all
#:   Pre-order        2,242   a subset, with no used listings in it at all
#:
#: So every narrow slice is read twice: once in its own lane, and again by the
#: full sweep. Worth paying for on pre-owned, which turns over hour to hour
#: and would otherwise wait a day to be noticed. Not obviously worth it on
#: in-stock and pre-order, which change slowly and the daily sweep covers
#: anyway - so those two start switched off, and the toggle is there for
#: anyone who wants the faster lane.
#:
#: Every slice is swept end to end on every pass. The cheaper design - re-read
#: the first pages often, the rest rarely - needs the shop to put new listings
#: at the front, and it does not, under either ordering it offers. Settled by
#: measurement over 24.7 hours covering a full Japanese working day, with the
#: whole slice enumerated at both ends so the arrivals were known exactly
#: rather than inferred (.probe/regtime_watch.py):
#:
#:   592 listings arrived. 9 of them - 2 per cent - ever appeared in the
#:   first twenty pages of "regtimed". None at all on the first two.
#:
#: Those twenty pages are 9.5 per cent of the slice, so a new listing is
#: about six times *less* likely to be found there than if the order were
#: shuffled. Price-range changes do slightly better, 19 per cent against the
#: 9.5 expected, but four fifths of them would still be missed.
#:
#: The same run measured the churn that sets the pace: 24 listings arrive and
#: 21 disappear every hour. An hourly full sweep therefore catches every
#: arrival within the hour, which is what the interval below is for.
DEFAULT_SCOPES: list[dict] = [
    {
        "scope": "figures_preowned",
        "label": "Pre-owned figures",
        "priority": 10,
        "query": {"category_id": 1, "condition": "preowned", "sort": "updated"},
        # Two settings, because there are two jobs. The short pass re-reads the
        # front of the "preowned" ordering, which is the one that actually
        # tracks intake - 30 pages every half hour, 60 requests an hour against
        # the 213 a full sweep costs. The full sweep behind it catches whatever
        # the front never showed, once a day. See _page_limit for what was
        # measured.
        "head_pages": 30,
        "recheck_interval_minutes": 30,
        "full_sweep_interval_minutes": 1440,
    },
    {
        "scope": "figures_in_stock",
        "label": "Figures in stock",
        "priority": 20,
        "query": {"category_id": 1, "stock_filter": "in_stock", "sort": "newest"},
        # 59 pages, about seven minutes. This is the only way a sold-out
        # listing coming back into stock gets noticed, which is why it earns a
        # frequent pass despite being small.
        "recheck_interval_minutes": 60,
    },
    {
        "scope": "figures_preorder",
        "label": "Figures on pre-order",
        "priority": 30,
        "query": {"category_id": 1, "stock_filter": "preorder", "sort": "newest"},
        # 45 pages. Announcements rather than restocks: a pre-order appears
        # once and then sits there, so a few hours of latency costs nothing.
        "recheck_interval_minutes": 180,
    },
    {
        "scope": "figures_all",
        "label": "All figures",
        "priority": 90,
        "query": {"category_id": 1, "sort": "newest"},
        # The backstop, not the workhorse: 1,385 pages, some three hours of
        # requests. Its job is to record a listing that appeared and sold out
        # between two passes, and to correct anything nothing else revisited.
        # Neither needs doing daily.
        "recheck_interval_minutes": 10080,
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
                recheck_interval_minutes=spec.get("recheck_interval_minutes", 30),
                head_pages=spec.get("head_pages", 0),
                full_sweep_interval_minutes=spec.get("full_sweep_interval_minutes"),
                enabled=spec.get("enabled", True),
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
    """How deep this cycle should go. Always: all the way.

    This used to re-read only the first ``head_pages`` of a slice between full
    sweeps, on the assumption that the shop lists newest first so the front of
    the list is where anything new turns up. Measured against the live API,
    that assumption is simply false for the slices this crawler reads.

    Measured on the pre-owned slice over nine hours (see .probe/order_probe.py,
    which records the snapshots this rests on):

      * 375 new listings appeared in that window. Not one of them landed on
        page one. The single arrival there had drifted up from page two
        because something above it sold out.
      * A product that took in five new copies - its intake counter went 120
        to 125 - did not move a single position. It sat at slot two on page
        one before and after.
      * The order itself is fixed: of 49 listings still on page one, zero had
        changed rank relative to each other. Pages only drift because entries
        above them leave, which is why page 50 kept two thirds of its content
        and page 100 kept none.

    So a listing's page says nothing about when it was added or when it was
    last restocked, and no sort key rescues the idea. That last part was then
    checked properly rather than assumed, over 24.7 hours covering a full
    Japanese working day, with the whole slice enumerated at both ends so the
    arrivals were known exactly (.probe/regtime_watch.py):

      * 592 listings arrived. 9 of them ever appeared in the first twenty
        pages of "regtimed", and none at all in the first two.
      * Those pages are 9.5 per cent of the slice, so an arrival is about six
        times *less* likely to be found there than under a shuffle. The
        ordering does not merely fail to help, it pushes new listings away
        from the front.
      * Price-range changes fare better and still badly: 19 per cent against
        the 9.5 expected, so four fifths would be missed.

    The practical effect was that after the first pass the crawler re-read the
    same thousand products for ever and found new listings only on the weekly
    sweep. Reading everything instead costs about the same: the whole
    pre-owned slice is 203 pages, roughly twenty-five minutes of requests at
    the configured rate, against the forty pages an hour the head passes were
    already spending on a fifth of it.

    ``head_pages`` is left on the model so an existing row still loads, but
    nothing reads it any more.

    That was measured on "regtimed", and the conclusion held for it. It does
    not hold for every ordering, which took a second round of measuring to
    establish. AmiAmi's own dropdown offers eight keys; asked about the same
    594 known arrivals, they differ enormously (.probe/intake_order.py):

      * "preowned" (中古) held 300 of them in its first twenty pages, 5.35
        times what a random slice of that size would catch
      * "releasedated" managed 1.30 times chance, the unsorted default 0.59
      * "regtimed" managed 0.20 - genuinely worse than a shuffle, which is
        what the 24.7-hour run had already shown from the other direction

    "regtime" is a real field and it does sort: read ascending it hands back
    the oldest product records first, ids around 1,100, and its last page is
    the first page of the descending order. It is simply the wrong field - it
    records when the *product* was registered, so a used copy taken in today
    attaches to a record made years ago and never moves to the front.

    Under "preowned" the recall by depth is (.probe/preowned_depth.py):

        10 pages   211 of 594   36%
        20 pages   300 of 594   51%
        30 pages   452 of 594   76%
        40 pages   452 of 594   76%   - not one more

    A plateau that flat is not an ordering missing things; it is the rest no
    longer being there. Of fourteen sampled arrivals the head had not shown,
    ten had already sold (.probe/missing_check.py), which puts recall among
    listings still on sale at about nine in ten - a small sample, so read that
    as "most" rather than as a precise figure.

    So a short pass is worth having again, on that ordering and no other. A
    slice sets ``head_pages`` to say how much of its front is worth re-reading
    often; zero means the ordering earns no such shortcut and every pass reads
    everything, which is where the other three slices stand.

    Column defaults only apply on insert, so the number is read defensively: a
    row that has not been flushed yet still carries None.
    """
    total = crawl.pages_total or 10_000  # unknown until the first response
    if full_sweep_due(crawl):
        return total
    head = crawl.head_pages or 0
    return min(total, head) if head > 0 else total


def full_sweep_interval_minutes(crawl: CatalogCrawl) -> int:
    """How long between passes that read the whole slice.

    Stored in minutes, falling back to the older days column so a row written
    by an earlier version keeps the schedule it was given.
    """
    minutes = crawl.full_sweep_interval_minutes
    if minutes:
        return max(1, minutes)
    return max(1, (crawl.full_sweep_interval_days or 7) * 1440)


def full_sweep_due(crawl: CatalogCrawl) -> bool:
    """Should this pass read everything rather than just the front?

    A slice with no head pass configured is always sweeping in full, so the
    question does not arise. Otherwise it is due when the last full sweep has
    aged out - and once a sweep has started, it finishes as a sweep: the check
    is against when the last one *completed*, so a pass that spans the moment
    the head interval elapses does not silently truncate itself half way.
    """
    if not (crawl.head_pages or 0):
        return True
    last = crawl.last_full_sweep_at
    if last is None:
        return True  # never swept: the first pass reads everything
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    due_at = last + timedelta(minutes=full_sweep_interval_minutes(crawl))
    if datetime.now(timezone.utc) >= due_at:
        return True
    # Already part way past the head, so this pass was started as a sweep.
    return (crawl.cursor_page or 1) > (crawl.head_pages or 0)


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


def _overdue_seconds(crawl: CatalogCrawl) -> float:
    """How long past its own schedule this slice is, negative if not yet due."""
    if not (crawl.cycles_completed or 0) or crawl.finished_at is None:
        return float("inf")  # never finished a pass, so always the first claim
    interval = timedelta(minutes=max(1, crawl.recheck_interval_minutes or 30))
    due = crawl.finished_at + interval
    return (datetime.now(timezone.utc) - due).total_seconds()


def _select_crawl(db: Session, provider: str) -> CatalogCrawl | None:
    """Which slice gets the next few minutes of crawling.

    Due work first, and it runs to completion; the long sweep fills whatever
    is left. Both halves of that came from getting it wrong.

    Strict priority starved everything below the busiest slice, which held the
    budget almost continuously. Plain rotation fixed the starving and replaced
    it with something worse to watch: every job picked whoever had waited
    longest, so sweeps were abandoned part way and resumed later and nothing
    ever visibly finished.

    So a slice that is past its own interval outranks one that is not, and
    stays outranking it until its pass completes - which is why a 211-page
    sweep now runs start to end rather than in scattered fragments. A slice
    only part way through and not yet due keeps going in the gaps, which is
    what the weekly full sweep does with the time the hourly ones leave.
    """
    candidates = list(
        db.execute(
            select(CatalogCrawl).where(
                CatalogCrawl.provider == provider,
                CatalogCrawl.enabled.is_(True),
                CatalogCrawl.consecutive_errors < 5,
            )
        )
        .scalars()
        .all()
    )

    def rank(crawl: CatalogCrawl) -> tuple:
        overdue = _overdue_seconds(crawl)
        mid_sweep = (crawl.cursor_page or 1) > 1
        # Due beats not due; among the due, the one waiting longest; among
        # those, the one already under way; then the configured priority.
        return (0 if overdue >= 0 else 1, -overdue, 0 if mid_sweep else 1, crawl.priority or 0)

    eligible = [
        crawl
        for crawl in candidates
        if _overdue_seconds(crawl) >= 0 or (crawl.cursor_page or 1) > 1
    ]
    if not eligible:
        return None
    return sorted(eligible, key=rank)[0]


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
    # Kept before it is overwritten: the gap since this slice last ran is what
    # turns pages-per-run into pages-per-hour of real time, and the idle part
    # of that gap is exactly what the old estimate pretended did not exist.
    previous_run_at = crawl.last_run_at
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
    # Measured before it is written down. This sat after _record_run, so
    # every logged run claimed to have taken 0.0 seconds and no run ever had
    # a pages-per-minute figure to show.
    run.seconds = time.monotonic() - started

    _record_throughput(crawl, run.pages, previous_run_at)
    _record_run(crawl, run)
    db.commit()

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


#: How many runs to keep per slice. Enough to see whether a slow pass was one
#: bad stretch or the normal speed, few enough that the row stays small.
RUN_LOG_LENGTH = 12


def _record_run(crawl: CatalogCrawl, run: "CrawlRun") -> None:
    """Keep what this run actually managed, newest first.

    The throughput here is pages per minute *while running*, which is a
    different question from the pages per hour used for estimates: this says
    how fast the slice moves when it has the budget, that one says how much
    real time a sweep will take including waiting for its turn. Both are worth
    seeing, and confusing them is how "about 33 minutes" came to mean an
    afternoon.
    """
    if run.pages <= 0 and not run.errors:
        return
    entry = {
        "at": utcnow().isoformat(),
        "seconds": round(run.seconds, 1),
        "pages": run.pages,
        "items": run.items,
        "new": run.new_items,
        "changed": run.changed,
        "stopped": run.stopped_because or None,
        "pages_per_minute": (
            round(run.pages / (run.seconds / 60.0), 1) if run.seconds > 1 else None
        ),
        "errors": len(run.errors),
    }
    # Reassigned rather than mutated: SQLAlchemy does not notice a list edited
    # in place on a JSON column, so appending would silently save nothing.
    crawl.recent_runs = ([entry] + list(crawl.recent_runs or []))[:RUN_LOG_LENGTH]


def _record_throughput(crawl: CatalogCrawl, pages: int, previous_run_at) -> None:
    """Fold this run into the slice's observed pages-per-hour.

    Measured against wall-clock time since the slice last ran, idle included,
    because that is the number an estimate needs: a slice waiting its turn is
    not making progress, however fast it moves when it is running.
    """
    if pages <= 0 or previous_run_at is None:
        return
    elapsed = (utcnow() - previous_run_at).total_seconds() / 3600.0
    # A gap long enough to be a restart rather than a rhythm says nothing
    # useful about throughput.
    if elapsed <= 0 or elapsed > 12:
        return
    observed = pages / elapsed
    crawl.pages_per_hour = (
        observed if crawl.pages_per_hour is None else crawl.pages_per_hour * 0.7 + observed * 0.3
    )


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
    # Whether this pass covered the whole slice or only its front. Read
    # before the cursor is reset, since that is what says how far it got.
    head = crawl.head_pages or 0
    was_full = head <= 0 or (crawl.cursor_page or 1) > head
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

    The count has to mean the same thing as the number it gets compared
    against, which is subtler than it looks. Every pre-owned listing carries
    the shop's in-stock flag - all hundred sampled did, since a used copy that
    sells is deleted rather than marked gone - while the in-stock slice
    filters on a different flag entirely, "first-hand stock available", which
    no used listing has. Counting our stored in_stock therefore swept the
    whole used catalogue into the in-stock total and reported 13,834 held
    against 2,813 listed, as though eleven thousand rows had gone stale.
    """
    from ..models import Condition

    stmt = select(func.count(Item.id)).where(Item.provider == provider_id)
    kind = SCOPE_FILTERS.get(scope, "all")
    if kind == "preowned":
        stmt = stmt.where(Item.condition == Condition.preowned)
    elif kind == "in_stock":
        # First-hand stock, which is what the slice asks the shop for.
        stmt = stmt.where(
            Item.in_stock.is_(True), Item.condition != Condition.preowned
        )
    elif kind == "preorder":
        stmt = stmt.where(
            Item.is_preorder.is_(True), Item.condition != Condition.preowned
        )
    return int(db.execute(stmt).scalar_one() or 0)


#: Where a reset of the activity profile is remembered. Nothing is deleted -
#: the numbers are derived from timestamps other features depend on - so a
#: reset is a line drawn under what came before.
ACTIVITY_SINCE_SETTING = "activity_since"


def activity_baseline(db: Session) -> datetime | None:
    """The point a reset drew a line at, if there has been one."""
    from ..models import AppSetting

    row = db.get(AppSetting, ACTIVITY_SINCE_SETTING)
    raw = (row.value or {}).get("at") if row else None
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def reset_activity(db: Session) -> datetime:
    """Ignore everything recorded so far when profiling the shop's rhythm.

    Worth having because the first catalogue build ruins the picture: every
    item is "first seen" while the crawler is working through the pages, so
    the busiest hour reads as whenever the sweep happened rather than when the
    shop was listing things. Once the catalogue is complete, drawing a line
    here gives a profile built only from listings that genuinely arrived.

    Nothing is deleted. The timestamps this reads belong to the price history
    and the catalogue, which have their own reasons to exist.
    """
    from ..models import AppSetting

    now = datetime.now(timezone.utc)
    row = db.get(AppSetting, ACTIVITY_SINCE_SETTING)
    if row is None:
        row = AppSetting(key=ACTIVITY_SINCE_SETTING, value={})
        db.add(row)
    row.value = {"at": now.isoformat()}
    db.commit()
    return now


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
    baseline = activity_baseline(db)
    if baseline is not None and baseline > since:
        since = baseline

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
        "since": since,
        "baseline": baseline,
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

    # Who would be picked next, by the same rule the scheduler uses, so the
    # view can say "waiting, third in line" instead of leaving someone to
    # wonder why a slice has not moved for an hour.
    waiting = sorted(
        (c for c in crawls if c.enabled and (c.consecutive_errors or 0) < 5),
        key=lambda c: (
            _cooldown_remaining(c) > 0,
            c.last_run_at or datetime.min.replace(tzinfo=timezone.utc),
            c.priority or 0,
        ),
    )
    queue = {c.scope: index for index, c in enumerate(waiting)}

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
                # Rows held here that the shop no longer counts in this slice:
                # a listing marked in stock stays that way until something
                # rechecks it, and until then the two numbers disagree.
                "stale_local": max(0, local - upstream) if upstream else 0,
                # Where it sits in the queue, and what the scheduler is doing.
                "queue_position": queue.get(crawl.scope),
                "resting": _cooldown_remaining(crawl) > 0,
                "sort_key": (crawl.query or {}).get("sort") or "newest",
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
                "eta_seconds": _eta_seconds(limit - done, crawl.pages_per_hour),
                "pages_per_hour": (
                    round(crawl.pages_per_hour, 1) if crawl.pages_per_hour else None
                ),
                "recent_runs": list(crawl.recent_runs or []),
                "next_run_in_seconds": _cooldown_remaining(crawl),
                "recheck_minutes": crawl.recheck_interval_minutes,
                "head_pages": crawl.head_pages,
                "full_sweep_interval_minutes": full_sweep_interval_minutes(crawl),
                # Which kind of pass this slice is on right now, so the view
                # can say "reading the newest 30" rather than showing a bar
                # that means two different things on alternate runs.
                "sweeping_all": full_sweep_due(crawl),
                "pages_this_pass": _page_limit(crawl),
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


def _eta_seconds(pages_remaining: int, pages_per_hour: float | None = None) -> int | None:
    """Time to finish, from what this slice actually manages per hour.

    The old version multiplied the configured request rate by the share of
    each interval spent crawling and called that an answer. It was optimistic
    by a wide margin, because it assumed the slice had the crawler to itself
    and that nothing ever paused: four slices share the time, the pacer
    scatters its requests and takes the occasional long break, the night slows
    everything by a factor of two and a half, and a due watch stops a run
    outright. A slice quoted at "about 33 minutes" was closer to an afternoon.

    So the measured rate is used when there is one, and the calculated figure
    only stands in until the first run has been through.
    """
    if pages_remaining <= 0:
        return None
    if pages_per_hour and pages_per_hour > 0:
        return int(pages_remaining / pages_per_hour * 3600)

    seconds_per_page = 60.0 / max(0.1, settings.crawler_requests_per_minute)
    working = seconds_per_page * pages_remaining
    duty = max(
        0.05,
        min(1.0, settings.crawler_max_seconds_per_run / (settings.crawler_run_interval_minutes * 60)),
    )
    # Divided again by the number of slices sharing the crawler, since the
    # rate above is what one gets with the whole thing to itself.
    contenders = max(1, len([s for s in DEFAULT_SCOPES if s.get("enabled", True)]))
    return int(working / duty * contenders)
