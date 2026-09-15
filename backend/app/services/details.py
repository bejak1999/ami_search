"""Filling in what one product fetch cannot say about a saved figure.

Two gaps, both caused by how the shop answers rather than by anything skipped.

A copy's note. Asked about a product, AmiAmi describes one copy of its own
choosing and lists the others with a price and a grade and nothing else.
Asked about FIGURE-149670-R it describes R569 - the tip of the flag is
detached - and says nothing about R599, whose skin is discoloured, or R606,
which is missing its acrylic stand. Those notes exist; the shop returns each
one only when that exact copy is asked for. So each copy is asked once, and
the answer is kept, because what the shop says about a copy does not change
while it is on sale.

The other listing. A figure is sold under two codes, and most of a wishlist
is saved from new listings that sold out long ago. The used listing can exist
without the catalogue ever having read it, and until it is known the wishlist
can neither show it nor count it as buyable.

Both are done for figures somebody saved or put an item watch on, and never
for the catalogue at large: the question is worth a request only when someone
is waiting on the answer.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    CollectionEntry,
    CollectionStatus,
    Item,
    Listing,
    ListingStatus,
    Watch,
    WatchKind,
    utcnow,
)
from ..providers import ItemNotFound, ProviderError, get_provider
from . import budget, catalog, reqlog
from .catalog import counterpart_code
from .shelflife import product_code_of

log = logging.getLogger(__name__)

#: How long "the shop has no listing under the other condition" is believed.
#: Used copies of a figure turn up months after the new one sells out, so the
#: answer goes stale - but not by the hour.
COUNTERPART_RECHECK = timedelta(days=7)

#: One background run's ceiling. Small on purpose: the job comes round every
#: few minutes, and a long wishlist is better finished over an afternoon than
#: allowed to stand between the sampler and its share of the allowance.
MAX_REQUESTS_PER_RUN = 60
MAX_SECONDS_PER_RUN = 240.0
ERRORS_BEFORE_GIVING_UP = 3

#: After an attempt from a page fails, how long before a page may start
#: another for the same figure. The item page looks again every few seconds
#: while notes are outstanding, and during an outage each look would otherwise
#: queue another round of requests against a shop that is refusing them.
RETRY_AFTER_FAILURE_SECONDS = 600.0

#: Bind parameters per IN clause. SQLite builds before 3.32 refuse more than
#: 999, and a wishlist with its counterparts can pass that.
_CHUNK = 500


def _chunks(values: list) -> list[list]:
    return [values[start : start + _CHUNK] for start in range(0, len(values), _CHUNK)]


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------------------
# Which figures
# ---------------------------------------------------------------------------


def saved_item_ids(db: Session) -> set[int]:
    """Every figure someone has wishlisted or put an item watch on.

    Both listings of each, because a copy's note matters on whichever of the
    two the copy is filed under, and that is rarely the one that was saved.
    """
    ids = {
        int(item_id)
        for item_id in db.execute(
            select(CollectionEntry.item_id).where(
                CollectionEntry.status == CollectionStatus.wishlist
            )
        ).scalars()
    }

    # A watch stores a code rather than an item, and a link copied from a
    # buying choice names one copy. Reduced to the product so it matches.
    watched: dict[str, set[str]] = {}
    for provider, code in db.execute(
        select(Watch.provider, Watch.item_code).where(
            Watch.kind == WatchKind.item,
            Watch.enabled.is_(True),
            Watch.item_code.is_not(None),
        )
    ).all():
        if code:
            watched.setdefault(provider, set()).add(product_code_of(code.upper()))
    for provider, codes in watched.items():
        for chunk in _chunks(sorted(codes)):
            ids.update(
                int(item_id)
                for item_id in db.execute(
                    select(Item.id).where(Item.provider == provider, Item.code.in_(chunk))
                ).scalars()
            )

    widened: set[int] = set()
    for chunk in _chunks(sorted(ids)):
        widened |= catalog.with_counterparts(db, chunk)
    return widened


# ---------------------------------------------------------------------------
# Copy notes
# ---------------------------------------------------------------------------


def _unasked(item_ids):
    return select(Listing).where(
        Listing.item_id.in_(item_ids),
        Listing.status == ListingStatus.live,
        Listing.note_checked_at.is_(None),
        # Used copies only. A new listing is one copy, and the product answer
        # already describes it, so asking again by code would buy nothing.
        Listing.sequence.is_not(None),
    )


def pending_copies(db: Session, item_ids, limit: int) -> list[Listing]:
    """Copies on sale that the shop has not yet been asked about by code."""
    ids = sorted({int(i) for i in item_ids if i})
    found: list[Listing] = []
    for chunk in _chunks(ids):
        if len(found) >= limit:
            break
        found.extend(
            db.execute(
                _unasked(chunk)
                .order_by(Listing.item_id, Listing.sequence)
                .limit(limit - len(found))
            ).scalars()
        )
    return found[:limit]


def pending_count(db: Session, item_id: int) -> int:
    return len(pending_copies(db, [item_id], limit=1000))


def ask_about_copy(db: Session, provider, listing: Listing) -> str:
    """Ask the shop about one copy by its own code, and keep what it says.

    Returns "noted", "silent" or "gone". A ProviderError is left to the
    caller: a request that failed has not answered anything, so the copy stays
    unasked and is tried again later.
    """
    try:
        answer = provider.get_item(listing.code)
    except ItemNotFound:
        # Sold since we last looked. Its note would describe nothing anyone
        # can buy, and asking again would only be told the same.
        listing.note_checked_at = utcnow()
        return "gone"

    described = (answer.condition_note_code or "").strip().upper()
    listing.note_checked_at = utcnow()
    if described != listing.code.strip().upper():
        # The shop described some other copy, which it does when the one asked
        # for is no longer what it has on sale under that code.
        return "gone"
    if answer.condition_note:
        listing.condition_note = answer.condition_note
        return "noted"
    # Asked, and nothing to say. A note stored from an earlier answer is kept:
    # an empty answer is not evidence that a fault has gone away.
    return "silent"


# ---------------------------------------------------------------------------
# The other listing
# ---------------------------------------------------------------------------


def _counterpart_due(item: Item, now: datetime) -> bool:
    checked = _aware(item.counterpart_checked_at)
    return checked is None or checked <= now - COUNTERPART_RECHECK


def needs_counterpart_lookup(db: Session, item: Item, now: datetime | None = None) -> bool:
    """Is the other listing of this figure unknown, and worth asking about?"""
    now = now or utcnow()
    return catalog.counterpart_of(db, item) is None and _counterpart_due(item, now)


def missing_counterparts(db: Session, limit: int, now: datetime | None = None) -> list[Item]:
    """Wishlisted figures whose other listing we have never seen.

    Never-asked first, then whichever was asked longest ago.
    """
    now = now or utcnow()
    ids = sorted(
        {
            int(item_id)
            for item_id in db.execute(
                select(CollectionEntry.item_id).where(
                    CollectionEntry.status == CollectionStatus.wishlist
                )
            ).scalars()
        }
    )
    due: list[Item] = []
    for chunk in _chunks(ids):
        items = list(db.execute(select(Item).where(Item.id.in_(chunk))).scalars())
        wanted: dict[str, set[str]] = {}
        for item in items:
            wanted.setdefault(item.provider, set()).add(counterpart_code(item.code))
        known: set[tuple[str, str]] = set()
        for provider, codes in wanted.items():
            known.update(
                (provider, code)
                for code in db.execute(
                    select(Item.code).where(
                        Item.provider == provider, Item.code.in_(sorted(codes))
                    )
                ).scalars()
            )
        due.extend(
            item
            for item in items
            if (item.provider, counterpart_code(item.code)) not in known
            and _counterpart_due(item, now)
        )

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    due.sort(key=lambda item: _aware(item.counterpart_checked_at) or epoch)
    return due[:limit]


def look_for_counterpart(db: Session, provider, item: Item) -> Item | None:
    """Ask the shop for this figure's other listing, and store it if it exists."""
    code = counterpart_code(item.code)
    try:
        answer = provider.get_item(code)
    except ItemNotFound:
        item.counterpart_checked_at = utcnow()
        return None
    other, _ = catalog.upsert_item(db, answer, commit=False)
    item.counterpart_checked_at = utcnow()
    return other


# ---------------------------------------------------------------------------
# The background run
# ---------------------------------------------------------------------------


def run_once(db: Session, provider_id: str = "amiami") -> dict:
    """Missing listings first, then copy notes, within one run's ceiling.

    Listings first because they change what the wishlist shows at all; a note
    only adds to a row that is already there.
    """
    from ..scheduler.engine import engine

    provider = get_provider(provider_id)
    deadline = time.monotonic() + MAX_SECONDS_PER_RUN
    result = {
        "counterparts_checked": 0,
        "counterparts_found": 0,
        "copies_checked": 0,
        "notes_found": 0,
        "errors": 0,
        "stopped_because": None,
    }
    spent = 0
    errors_in_a_row = 0

    def reason_to_stop() -> str | None:
        if getattr(engine, "stopping", False):
            return "shutting down"
        if time.monotonic() >= deadline:
            return "time budget reached"
        if spent >= MAX_REQUESTS_PER_RUN:
            return "request budget reached"
        if errors_in_a_row >= ERRORS_BEFORE_GIVING_UP:
            return "too many upstream errors in a row"
        return None

    for item in missing_counterparts(db, MAX_REQUESTS_PER_RUN):
        result["stopped_because"] = reason_to_stop()
        if result["stopped_because"]:
            return result
        spent += 1
        try:
            found = look_for_counterpart(db, provider, item)
        except ProviderError as exc:
            db.rollback()
            errors_in_a_row += 1
            result["errors"] += 1
            log.debug("Counterpart lookup failed for %s: %s", item.code, exc)
            continue
        errors_in_a_row = 0
        result["counterparts_checked"] += 1
        if found is not None:
            result["counterparts_found"] += 1
        db.commit()

    for listing in pending_copies(db, saved_item_ids(db), MAX_REQUESTS_PER_RUN - spent):
        result["stopped_because"] = reason_to_stop()
        if result["stopped_because"]:
            return result
        spent += 1
        try:
            outcome = ask_about_copy(db, provider, listing)
        except ProviderError as exc:
            db.rollback()
            errors_in_a_row += 1
            result["errors"] += 1
            log.debug("Note lookup failed for %s: %s", listing.code, exc)
            continue
        errors_in_a_row = 0
        result["copies_checked"] += 1
        if outcome == "noted":
            result["notes_found"] += 1
        db.commit()

    result["stopped_because"] = reason_to_stop() or "nothing left to ask"
    return result


# ---------------------------------------------------------------------------
# Started from a page
# ---------------------------------------------------------------------------

_busy_lock = threading.Lock()
_busy: set[tuple[str, int]] = set()
_not_before: dict[tuple[str, int], float] = {}


def _begin(key: tuple[str, int]) -> bool:
    with _busy_lock:
        if key in _busy or _not_before.get(key, 0.0) > time.monotonic():
            return False
        _busy.add(key)
        return True


def _end(key: tuple[str, int], failed: bool) -> None:
    with _busy_lock:
        _busy.discard(key)
        if failed:
            _not_before[key] = time.monotonic() + RETRY_AFTER_FAILURE_SECONDS
        else:
            _not_before.pop(key, None)


def fill_item_notes_now(item_id: int) -> None:
    """Ask about this figure's unasked copies, for a page that is open on it.

    Run after the response has gone, so the page is not held for the length
    of eight requests; it looks again while notes are outstanding.
    """
    from ..db import session_scope

    key = ("notes", int(item_id))
    if not _begin(key):
        return
    failed = False
    try:
        with session_scope() as db, reqlog.purpose("manual"), budget.claim("manual"):
            item = db.get(Item, item_id)
            if item is None:
                return
            provider = get_provider(item.provider)
            for listing in pending_copies(db, [item.id], limit=1000):
                try:
                    ask_about_copy(db, provider, listing)
                except ProviderError as exc:
                    log.debug("Note lookup failed for %s: %s", listing.code, exc)
                    db.rollback()
                    failed = True
                    break
                db.commit()
    except Exception:  # noqa: BLE001 - runs after the response; nobody to tell
        log.exception("Filling in copy notes for item %s failed", item_id)
        failed = True
    finally:
        _end(key, failed)


def look_for_counterpart_now(item_id: int) -> None:
    """Look for the other listing of a figure that has just been saved."""
    from ..db import session_scope

    key = ("counterpart", int(item_id))
    if not _begin(key):
        return
    failed = False
    try:
        with session_scope() as db, reqlog.purpose("manual"), budget.claim("manual"):
            item = db.get(Item, item_id)
            if item is None or not needs_counterpart_lookup(db, item):
                return
            try:
                look_for_counterpart(db, get_provider(item.provider), item)
            except ProviderError as exc:
                log.debug("Counterpart lookup failed for %s: %s", item.code, exc)
                db.rollback()
                failed = True
                return
            db.commit()
    except Exception:  # noqa: BLE001 - runs after the response; nobody to tell
        log.exception("Looking for the other listing of item %s failed", item_id)
        failed = True
    finally:
        _end(key, failed)
