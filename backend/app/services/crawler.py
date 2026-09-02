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
from . import budget, reqlog
from ..models import CatalogCrawl, CrawlState, Item, Watch, utcnow
from ..providers import ProviderError, SearchQuery, get_provider
# The shop's own name for each ordering, so the debug view can say which one
# a slice is reading rather than only our label for it.
from ..providers.amiami import SORT_KEYS
from . import catalog
from .pacing import HumanPacer

log = logging.getLogger(__name__)

#: The slices we build, best first. Pre-owned moves fastest and is what people
#: actually hunt, so it fills in before the much larger long tail.
#: How each slice's membership is expressed as a local query, so coverage can
#: be counted from the database instead of from a counter that only ever grows.
#: Orderings whose first pages are worth re-reading between full sweeps.
#: Measured, not assumed: against 594 known arrivals the first 20 pages of
#: "updated" (AmiAmi's 中古) held 300 of them, "release" 1.30x chance, the
#: unsorted default 0.59x, and "newest" (regtimed) 0.20x - worse than a
#: shuffle. Anything not in here reads all of itself, every pass.
HEAD_WORTH_READING = frozenset({"updated", "preowned"})

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
        # The only slice where time matters. A used copy can be listed and
        # sold inside a morning, so the front of the "preowned" ordering - the
        # one that actually tracks intake - is re-read hourly, and the whole
        # slice once a day behind it. See _page_limit for what was measured.
        "head_pages": 30,
        "recheck_interval_minutes": 60,
        "full_sweep_interval_minutes": 1440,
    },
    {
        "scope": "figures_in_stock",
        "label": "Figures in stock",
        "priority": 20,
        "query": {"category_id": 1, "stock_filter": "in_stock", "sort": "newest"},
        # 59 pages, about seven minutes, read end to end - nothing useful sits
        # at the front of this ordering, so there is no short pass to make. A
        # first-hand listing coming back into stock is not a race: the stock is
        # replenished, not a single copy, so a day's latency costs nothing.
        "recheck_interval_minutes": 1440,
    },
    {
        "scope": "figures_preorder",
        "label": "Figures on pre-order",
        "priority": 30,
        "query": {"category_id": 1, "stock_filter": "preorder", "sort": "newest"},
        # 45 pages. Announcements rather than restocks: a pre-order appears
        # once and then sits there for months, so a day of latency costs
        # nothing at all.
        "recheck_interval_minutes": 1440,
    },
    {
        "scope": "figures_all",
        "label": "All figures",
        "priority": 90,
        "query": {"category_id": 1, "sort": "newest"},
        # The backstop, not the workhorse: 1,385 pages, some three hours of
        # requests. Its job is to record a listing that appeared and sold out
        # between two passes, and to correct anything nothing else revisited.
        # Neither needs doing often; a fortnight is plenty.
        "recheck_interval_minutes": 20160,
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
    #: How often this run stood aside for a watch and picked up again.
    interruptions: int = 0
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
        # Drawn from the shared pool rather than fixed: with nothing else
        # running a sweep gets the lot, and steps back when a watch does.
        rate_source=lambda: budget.rate_for("catalogue"),
        sigma=settings.crawler_jitter_sigma,
        break_probability=settings.crawler_break_probability,
        quiet_hours=(settings.crawler_quiet_hours_start, settings.crawler_quiet_hours_end),
        quiet_slowdown=settings.crawler_quiet_slowdown,
    )


#: How long to hold still while a watch takes its turn, and how often to
#: look again. The scheduler polls due watches every five seconds, so waiting
#: in shorter steps than that only burns queries.
_YIELD_STEP_SECONDS = 2.0


def _wait_for_watches(db: Session, deadline: float) -> bool:
    """Hold still until no watch is waiting. False if the budget ran out.

    The session is expired between looks so the next one reads what the watch
    poller has committed rather than this transaction's snapshot - otherwise
    the crawler would wait for a state it can no longer see change.
    """
    from ..scheduler.engine import engine

    while True:
        if time.monotonic() >= deadline or getattr(engine, "stopping", False):
            return False
        time.sleep(min(_YIELD_STEP_SECONDS, max(0.0, deadline - time.monotonic())))
        db.expire_all()
        if not watches_are_due(db):
            return True


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
    head = crawl.head_pages or 0
    if head <= 0:
        return total
    # Between passes there is nothing in progress to confuse a sweep with, so
    # the schedule answers and the view can say how long the next pass will
    # be. Once a pass is under way the flag answers, because the cursor alone
    # cannot tell a finished short pass from a sweep at the same page.
    #
    # Keyed on whether a pass is under way. Either signal says so: the
    # accumulator, written when a pass begins and cleared when it ends, and a
    # cursor that has left page one.
    #
    # The cursor alone was standing in for this and cannot cover the start of
    # a pass: at page 1 a pass has begun but the cursor has not moved, so the
    # schedule answered instead of the flag - and a pass started by hand
    # could not choose its own kind, because asking for a short one while a
    # full sweep was due produced a full one anyway. The accumulator alone
    # would not cover a pass that began under a build that did not keep one.
    in_progress = bool(crawl.current_pass) or (crawl.cursor_page or 1) > 1
    sweeping = crawl.sweeping_all if in_progress else full_sweep_due(crawl)
    return total if sweeping else min(total, head)


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
    """Is a pass over the whole slice owed?

    Asked once, when a pass begins. A slice with no short pass configured is
    always sweeping in full, so the question does not arise for it.

    This deliberately says nothing about the pass currently running - see
    ``sweeping_all`` for that. An earlier version tried to infer it from the
    cursor, on the reasoning that a cursor past the front meant a sweep
    already under way. It does not distinguish the two cases: a short pass
    reaching page 31 of a 30-page front looks identical to a sweep at page 31,
    so every short pass flipped into a full one at its own boundary and read
    all 213 pages. The saving the short pass exists for was silently undone.
    """
    if not (crawl.head_pages or 0):
        return True
    last = crawl.last_full_sweep_at
    if last is None:
        return True  # never swept: the first pass reads everything
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= last + timedelta(
        minutes=full_sweep_interval_minutes(crawl)
    )


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


#: Consecutive failures before a slice is rested rather than retried.
ERROR_PATIENCE = 5
#: How long it rests, doubling with each further run of failures, capped.
ERROR_REST_MINUTES = 15
ERROR_REST_MAX_MINUTES = 6 * 60


def error_rest_seconds(crawl: CatalogCrawl) -> float:
    """How much longer this slice is being rested, zero if it is not.

    A slice used to be struck off the list outright once it had failed five
    times in a row - and that is a trap it cannot get out of, because a slice
    that is never selected can never succeed, and only a success clears the
    counter. One bad patch of network and the slice stops for good, silently,
    with the panel still saying "running" from whenever it last did.

    So it rests instead. The rest doubles with each further run of failures,
    up to a few hours, and then it is tried again.
    """
    failures = crawl.consecutive_errors or 0
    if failures < ERROR_PATIENCE or crawl.last_error_at is None:
        return 0.0
    since = crawl.last_error_at
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    minutes = min(
        ERROR_REST_MAX_MINUTES,
        ERROR_REST_MINUTES * (2 ** (failures - ERROR_PATIENCE)),
    )
    remaining = (since + timedelta(minutes=minutes)) - datetime.now(timezone.utc)
    return max(0.0, remaining.total_seconds())


def resting_after_errors(crawl: CatalogCrawl) -> bool:
    return error_rest_seconds(crawl) > 0


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
    candidates = [
        crawl
        for crawl in db.execute(
            select(CatalogCrawl).where(
                CatalogCrawl.provider == provider,
                CatalogCrawl.enabled.is_(True),
            )
        )
        .scalars()
        .all()
        if not resting_after_errors(crawl)
    ]

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


def start_pass(db: Session, crawl: CatalogCrawl, full: bool) -> None:
    """Begin a pass of the given kind on this slice, now.

    For the buttons in the admin view. Whatever was in progress is abandoned -
    that is what asking for a fresh pass means - and the kind is settled here
    rather than left to the schedule, so asking for a short pass while a full
    sweep happens to be due gets a short pass.

    The timers are not touched. They are reset when a pass *finishes*, by
    _complete_cycle, and that distinction matters: a short pass started by
    hand must not postpone a full sweep that is already owed, and a pass that
    is abandoned half way should not leave the schedule believing it ran.
    """
    crawl.cursor_page = 1
    crawl.sweeping_all = full
    crawl.state = CrawlState.idle
    crawl.consecutive_errors = 0
    crawl.last_error = None
    crawl.last_error_at = None
    # Declaring the pass here is what makes the kind stick: _page_limit reads
    # the flag for as long as this is set, and the crawler leaves an already
    # declared pass alone rather than asking the schedule again.
    crawl.current_pass = {
        "started_at": utcnow().isoformat(),
        "pages": 0,
        "items": 0,
        "new": 0,
        "changed": 0,
        "errors": 0,
        "slots": 0,
        "working_seconds": 0.0,
        "interruptions": 0,
    }
    # Nothing to wait for: an unfinished slice is always the first claim, so
    # this jumps the queue rather than sitting behind whatever is due.
    crawl.finished_at = None
    db.commit()


def run_once(
    db: Session,
    provider_id: str = "amiami",
    budget_seconds: int | None = None,
    scope: str | None = None,
) -> CrawlRun:
    """Crawl for a while, then stop and leave the cursor where it is.

    ``scope`` names one slice instead of letting the usual ranking choose,
    which is what the admin view's buttons need: "sweep this one now" cannot
    be expressed by asking whoever is next in line.
    """
    run = CrawlRun()
    if not settings.crawler_enabled:
        run.stopped_because = "disabled"
        return run

    ensure_scopes(db, provider_id)
    if scope is not None:
        crawl = db.execute(
            select(CatalogCrawl).where(
                CatalogCrawl.provider == provider_id, CatalogCrawl.scope == scope
            )
        ).scalar_one_or_none()
    else:
        crawl = _select_crawl(db, provider_id)
    if crawl is None:
        run.stopped_because = "no slice available"
        return run

    run.scope = crawl.scope
    # Set here rather than by the scheduler, because only now is it known
    # which slice this run belongs to - and four slices sharing one purpose
    # is what made a per-slice debug view show whichever ran last.
    with reqlog.purpose("catalogue", tag=crawl.scope):
        return _crawl_slice(db, provider_id, crawl, run, budget_seconds)


def _crawl_slice(
    db: Session,
    provider_id: str,
    crawl: CatalogCrawl,
    run: CrawlRun,
    budget_seconds: int | None,
) -> CrawlRun:
    """One slice's turn, with its requests already attributed to it."""
    provider = get_provider(provider_id)
    pacer = _pacer()
    #: Whether this slot took the pass to its end. Decided in the loop, acted
    #: on after the accounting, so the pass is complete before it is written.
    finished = False
    deadline = time.monotonic() + (budget_seconds or settings.crawler_max_seconds_per_run)
    started = time.monotonic()

    if crawl.started_at is None:
        crawl.started_at = utcnow()
    if (crawl.cursor_page or 1) <= 1 and not crawl.current_pass:
        # A pass begins here, and what kind it is has to be settled now: the
        # cursor cannot tell them apart later, and a pass that changed its
        # mind half way would either stop a sweep short or turn a short pass
        # into a sweep.
        #
        # Unless one has already been declared - a sweep started by hand says
        # which kind it is when the button is pressed, and the schedule must
        # not overrule the person who asked.
        crawl.sweeping_all = full_sweep_due(crawl)
        crawl.current_pass = {
            "started_at": utcnow().isoformat(),
            "pages": 0,
            "items": 0,
            "new": 0,
            "changed": 0,
            "errors": 0,
            "slots": 0,
            "working_seconds": 0.0,
            "interruptions": 0,
        }
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
            # Stand aside for the watch, then carry on within the same budget.
            #
            # This used to end the run outright, which sounds like the same
            # thing and is not: the crawler is given four minutes every five,
            # and abandoning that at the ten-second mark forfeited the rest of
            # it. Watches poll every minute or two, so nearly every run ended
            # within seconds - measured over an hour, eleven runs covered 176
            # pages where the budget allowed 470, and a twenty-page pass took
            # the hour instead of the two and a half minutes it costs.
            #
            # The watch still goes first. Only the waiting is no longer paid
            # for twice.
            run.interruptions += 1
            if not _wait_for_watches(db, deadline):
                run.stopped_because = "time budget reached"
                break
            continue

        limit = _page_limit(crawl)
        reqlog.doing(
            "catalogue",
            f"{crawl.label or crawl.scope}: page {crawl.cursor_page} of {limit}",
            tag=crawl.scope,
            slice=crawl.scope,
            sort=(crawl.query or {}).get("sort") or "newest",
            sort_key=SORT_KEYS.get((crawl.query or {}).get("sort") or "newest", "?"),
            page=crawl.cursor_page,
            pages=limit,
            sweeping=bool(crawl.sweeping_all),
            pass_number=(crawl.cycles_completed or 0) + 1,
        )
        if crawl.cursor_page > limit:
            finished = True
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
            crawl.last_error_at = utcnow()
            run.errors.append(str(exc))
            db.commit()
            run.stopped_because = "upstream error"
            break

        crawl.consecutive_errors = 0
        crawl.last_error = None
        crawl.last_error_at = None
        if result.total:
            crawl.total_results = result.total
            crawl.pages_total = max(1, math.ceil(result.total / max(1, crawl.per_page)))

        if not result.items:
            # Ran off the end sooner than the reported total suggested.
            finished = True
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

    # This slot is folded in first, and only then is the pass closed. The
    # other way round - which is how this was written - closed the pass,
    # cleared the accumulator, and then added the final slot's pages to the
    # *next* pass. Every logged pass came out short by however much it did
    # last, which is why a thirty-page short pass was recorded as 13, 22, 26,
    # anything but thirty.
    _accumulate(crawl, run)
    # After the slot is folded in, so the measurement covers the pass as it
    # actually stands rather than the slot in isolation.
    _record_throughput(crawl)
    if finished:
        _complete_cycle(db, crawl)
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


def _accumulate(crawl: CatalogCrawl, run: "CrawlRun") -> None:
    """Fold one scheduler slot into the pass it belongs to.

    Nothing is written to the log here. A pass is what someone means by "a
    crawl" - the front-to-back read - and it is spread over however many slots
    the scheduler gives it. Logging each slot filled the history with
    four-second fragments and a pages-per-minute figure that regularly
    exceeded the configured rate limit, because two pages fetched from banked
    tokens is not a speed anything can hold.
    """
    state = dict(crawl.current_pass or {})
    if not state:
        # A slot that arrived without a pass start behind it - a resumed
        # cursor after a restart. Begin the accounting from here rather than
        # dropping the work on the floor.
        state = {
            "started_at": utcnow().isoformat(),
            "pages": 0, "items": 0, "new": 0, "changed": 0,
            "errors": 0, "slots": 0, "working_seconds": 0.0, "interruptions": 0,
        }
    state["pages"] = state.get("pages", 0) + run.pages
    state["items"] = state.get("items", 0) + run.items
    state["new"] = state.get("new", 0) + run.new_items
    state["changed"] = state.get("changed", 0) + run.changed
    state["errors"] = state.get("errors", 0) + len(run.errors)
    state["interruptions"] = state.get("interruptions", 0) + run.interruptions
    state["slots"] = state.get("slots", 0) + 1
    state["working_seconds"] = round(state.get("working_seconds", 0.0) + run.seconds, 1)
    # Reassigned rather than mutated: SQLAlchemy does not notice a dict edited
    # in place on a JSON column, so the update would silently save nothing.
    crawl.current_pass = state


def _record_pass(crawl: CatalogCrawl, ended_because: str) -> None:
    """Write down one whole pass, front to back.

    Two durations, because they answer different questions. The elapsed time
    is how long the pass took in the world - start to finish, waiting
    included - which is what someone means by "how long did the crawl take".
    The working time is how much of that it spent fetching; the rest was
    spent waiting for its turn behind watches and between scheduler slots.
    Only the first of those makes pages-per-minute mean anything.
    """
    state = dict(crawl.current_pass or {})
    if not state or not state.get("pages"):
        # Nothing worth a log entry, but the accumulator still has to be
        # cleared: it is what says a pass is under way, and a pass that
        # fetched nothing and left its marker behind would look like one
        # still running for ever after.
        crawl.current_pass = {}
        return

    started = state.get("started_at")
    finished = utcnow()
    try:
        began = datetime.fromisoformat(started) if started else finished
    except ValueError:  # pragma: no cover - a hand-edited row
        began = finished
    if began.tzinfo is None:
        began = began.replace(tzinfo=timezone.utc)

    elapsed = max(0.0, (finished - began).total_seconds())
    working = float(state.get("working_seconds") or 0.0)
    pages = int(state.get("pages") or 0)

    entry = {
        "at": finished.isoformat(),
        "started_at": began.isoformat(),
        # Front to back, waiting included.
        "seconds": round(elapsed, 1),
        # Of which actually fetching.
        "working_seconds": round(min(working, elapsed), 1),
        "waiting_seconds": round(max(0.0, elapsed - working), 1),
        "slots": int(state.get("slots") or 0),
        "interruptions": int(state.get("interruptions") or 0),
        "pages": pages,
        "items": int(state.get("items") or 0),
        "new": int(state.get("new") or 0),
        "changed": int(state.get("changed") or 0),
        "errors": int(state.get("errors") or 0),
        "stopped": ended_because or None,
        # Over the whole pass, so it is a rate the slice can actually hold.
        "pages_per_minute": round(pages / (elapsed / 60.0), 1) if elapsed >= 30 else None,
    }
    crawl.recent_runs = ([entry] + list(crawl.recent_runs or []))[:RUN_LOG_LENGTH]
    crawl.current_pass = {}


def _record_throughput(crawl: CatalogCrawl) -> None:
    """Fold the pass so far into the slice's observed pages-per-hour.

    Measured over the pass itself: from when it started to now, including the
    waits between the scheduler slots it is spread over, because those are
    part of how long a pass takes. What it must *not* include is the cooldown
    before the pass began.

    That was the previous measure - wall clock since the slice last ran - and
    for a short pass it timed the wrong thing entirely. A thirty-page pass
    that runs once an hour and finishes in two minutes came out at thirty
    pages an hour, so the panel promised "this sweep done in about an hour"
    for work that was over before anyone read the sentence. It was quoting
    the cadence, not the pass.
    """
    state = dict(crawl.current_pass or {})
    pages = int(state.get("pages") or 0)
    started = state.get("started_at")
    if pages <= 0 or not started:
        return
    try:
        began = datetime.fromisoformat(started)
    except ValueError:  # pragma: no cover - a hand-edited row
        return
    if began.tzinfo is None:
        began = began.replace(tzinfo=timezone.utc)

    hours = (utcnow() - began).total_seconds() / 3600.0
    # Too short to divide by: a pass one slot old has fetched its pages in
    # seconds, and extrapolating that to an hourly rate produces a number in
    # the thousands. The next slot will have a usable span.
    if hours < 30 / 3600.0:
        return
    observed = pages / hours
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
    # What kind of pass this was, as decided when it started.
    was_full = bool(crawl.sweeping_all) or not (crawl.head_pages or 0)
    _record_pass(crawl, "read to the end")
    crawl.cycles_completed = (crawl.cycles_completed or 0) + 1
    crawl.cursor_page = 1
    crawl.finished_at = utcnow()
    crawl.state = CrawlState.completed
    crawl.sweeping_all = False
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


def local_count(
    db: Session, scope: str, provider_id: str, live_only: bool = False
) -> int:
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
    if live_only:
        # Only what the shop could still be listing. Keeping the record of a
        # sold-out listing is the point of the application, but it is not
        # something the shop counts, so including it in the comparison made
        # coverage meaningless: once the kept records outnumbered the live
        # ones the ratio pinned itself at 100% and stayed there, whether we
        # held everything currently on sale or half of it.
        stmt = stmt.where(Item.order_closed.is_(False))
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

    def buckets(column, field: str, extra=None) -> dict[int, int]:
        # extract() rather than strftime(): the same query has to run on
        # PostgreSQL, which this application supports and which has no such
        # function - the endpoint raised there rather than returning a
        # profile. SQLAlchemy compiles this to STRFTIME on SQLite and to
        # EXTRACT on PostgreSQL, and "dow" numbers the week from Sunday on
        # both, which is what the view expects.
        stmt = select(
            func.cast(func.extract(field, column), Integer).label("bucket"),
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

    listings_hour = buckets(Item.first_seen_at, "hour", Item.provider == provider_id)
    listings_day = buckets(Item.first_seen_at, "dow", Item.provider == provider_id)
    changes_hour = buckets(PricePoint.recorded_at, "hour")
    changes_day = buckets(PricePoint.recorded_at, "dow")

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
        # which is how both engines number the week.
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
        (c for c in crawls if c.enabled and not resting_after_errors(c)),
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
        live = local_count(db, crawl.scope, provider_id, live_only=True)
        upstream = crawl.total_results or 0
        slices.append(
            {
                "scope": crawl.scope,
                # Measured over what the shop could still be listing, so this
                # answers "how much of the shop do we hold" rather than
                # "do we hold more rows than the shop has listings", which is
                # true of every mature installation and says nothing.
                "coverage_percent": (
                    round(min(live, upstream) / upstream * 100, 1) if upstream else 0.0
                ),
                # Records of listings the shop has taken down. Not a fault -
                # keeping them is why this exists - and counted separately
                # from rows whose status simply has not been rechecked.
                "removed_local": max(0, local - live),
                # Rows we still believe are on sale that the shop does not
                # list. These are the ones a sweep corrects.
                "stale_local": max(0, live - upstream) if upstream else 0,
                # Where it sits in the queue, and what the scheduler is doing.
                "queue_position": queue.get(crawl.scope),
                "resting": _cooldown_remaining(crawl) > 0,
                # Whether a short pass means anything here. Only for the
                # second-hand slice: it is the only one where a listing can
                # appear and sell inside a morning, and the only one whose
                # ordering carries new intake to the front.
                #
                # Keyed on what the slice *contains*, not on its sort key.
                # The sort key version of this went false on an installation
                # whose query held something else, and took the settings away
                # from the one slice they belong to. The scope cannot drift.
                "head_supported": SCOPE_FILTERS.get(crawl.scope) == "preowned",
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
                # Which ordering this slice actually reads. Shown because the
                # short pass only pays on one of them, so a head depth set
                # against the wrong ordering is effort spent on nothing - and
                # until now the only way to find out was to catch the slice
                # mid-run in the debug view.
                "sort": (crawl.query or {}).get("sort") or "newest",
                "sort_key": SORT_KEYS.get(
                    (crawl.query or {}).get("sort") or "newest", "?"
                ),
                "head_worth_reading": (
                    ((crawl.query or {}).get("sort") or "newest") in HEAD_WORTH_READING
                ),
                "full_sweep_interval_minutes": full_sweep_interval_minutes(crawl),
                # Which kind of pass this slice is on right now, so the view
                # can say "reading the newest 30" rather than showing a bar
                # that means two different things on alternate runs.
                "sweeping_all": bool(crawl.sweeping_all) or not (crawl.head_pages or 0),
                "resting_seconds": round(error_rest_seconds(crawl)),
                "consecutive_errors": crawl.consecutive_errors or 0,
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
        # What the crawler may use right now, not the per-job setting it no
        # longer reads. Same trap the shelf panel was in: the number on
        # screen described an arrangement that had been replaced.
        "requests_per_minute": round(budget.rate_for("catalogue"), 1),
        "budget_per_minute": budget.total_per_minute(),
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

    # From the shared pool, because that is what paces a sweep now. Reading
    # the old per-job setting made this quote a sweep at eight pages a minute
    # when it can have three times that with the shop to itself - an estimate
    # wrong in the direction that matters, since it promised longer than the
    # truth and so was never questioned.
    seconds_per_page = 60.0 / max(0.1, budget.rate_for("catalogue"))
    working = seconds_per_page * pages_remaining
    duty = max(
        0.05,
        min(1.0, settings.crawler_max_seconds_per_run / (settings.crawler_run_interval_minutes * 60)),
    )
    # Divided again by the number of slices sharing the crawler, since the
    # rate above is what one gets with the whole thing to itself.
    contenders = max(1, len([s for s in DEFAULT_SCOPES if s.get("enabled", True)]))
    return int(working / duty * contenders)
