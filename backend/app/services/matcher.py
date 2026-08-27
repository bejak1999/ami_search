"""Watch evaluation: poll a shop, decide what is worth an alert, send it.

One rule drives the whole module: an alert must be *new information*. Seeing
the same listing at the same price on the next poll is not news, so every
match is diffed against a per-watch memory (:class:`WatchSeenItem`) before it
can become an alert.
"""
from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Alert,
    Condition,
    CostProfile,
    Item,
    PriceBasis,
    TriggerType,
    User,
    Watch,
    WatchKind,
    WatchSeenItem,
    utcnow,
)
from ..providers import ItemNotFound, ProviderError, SearchQuery, get_provider
from ..providers.amiami import meets_grade
from . import catalog, fx, landed_cost, notify

log = logging.getLogger(__name__)

NEWLINE = chr(10)

#: Never send more than this many individual alerts from a single poll.
#: Anything beyond it is folded into one summary alert.
MAX_ALERTS_PER_RUN = 8

#: Highest priority first. At most one alert per item per run.
TRIGGER_PRIORITY = (
    TriggerType.price_below,
    TriggerType.restock_preowned,
    TriggerType.back_in_stock,
    TriggerType.price_drop,
    TriggerType.new_match,
)


@dataclass
class RunOutcome:
    watch_id: int
    checked: int = 0
    matched: int = 0
    alerts: int = 0
    error: str | None = None
    took_ms: int = 0
    next_run_at: datetime | None = None
    triggers: list[str] = field(default_factory=list)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def build_query(watch: Watch) -> SearchQuery:
    filters = watch.filters or {}
    return SearchQuery(
        keywords=watch.query or "",
        page=1,
        per_page=int(filters.get("per_page") or 50),
        condition=watch.condition.value,
        stock_filter=watch.stock_filter.value,
        sort=filters.get("sort") or ("preowned" if watch.condition == Condition.preowned else "newest"),
        category_id=filters.get("category_id"),
        maker_id=filters.get("maker_id"),
        series_id=filters.get("series_id"),
        character_id=filters.get("character_id"),
        min_price=filters.get("min_price"),
        max_price=filters.get("max_price"),
        exclude_keywords=filters.get("exclude_keywords") or [],
        extra=filters.get("extra") or {},
    )


def _profile(db: Session, user: User) -> CostProfile:
    profile = user.cost_profile
    if profile is None:
        profile = landed_cost.default_profile(user.id)
        db.add(profile)
        db.commit()
    return profile


#: A watch with grade filters needs the graded sub-listings, which only the
#: detail endpoint returns. This caps how many extra detail calls one run may
#: spend resolving them.
MAX_GRADE_LOOKUPS_PER_RUN = 8


def qualifying_variant(item: Item, watch: Watch) -> dict | None:
    """The cheapest sub-listing that satisfies the watch's grade filters.

    Returns None when the item has graded listings but none of them are good
    enough, which correctly means "no match" rather than "match at the wrong
    grade".
    """
    variants = item.variants or []
    if not variants:
        return None
    for variant in sorted(variants, key=lambda v: v.get("price") or 0):
        if not meets_grade(variant.get("item_grade"), watch.min_item_grade):
            continue
        if not meets_grade(variant.get("box_grade"), watch.min_box_grade):
            continue
        return variant
    return None


def effective_price(item: Item, watch: Watch) -> tuple[float | None, dict | None]:
    """The price this watch should judge, plus the listing it came from.

    Without grade filters that is simply the cheapest listing, which is what
    ``item.current_price`` already holds.
    """
    if not (watch.min_item_grade or watch.min_box_grade):
        return item.current_price, None
    variant = qualifying_variant(item, watch)
    if variant is None:
        # Either nothing is good enough, or the graded listings have not been
        # fetched yet. Both mean this item cannot be said to match.
        return None, None
    return variant.get("price"), variant


@dataclass(slots=True)
class Valuation:
    """An item's price expressed in the currency the watch compares against."""

    compare_price: float | None
    compare_currency: str
    landed_total: float | None
    landed_currency: str
    breakdown: dict | None
    #: The shop price actually judged, which is the cheapest listing meeting
    #: the watch's grade filters rather than necessarily the headline price.
    shop_price: float | None = None
    #: The graded sub-listing it came from, when grade filters are in use.
    variant: dict | None = None


def value_item(db: Session, item: Item, watch: Watch, user: User) -> Valuation:
    """Convert an item's shop price into the watch's comparison basis."""
    profile = _profile(db, user)
    target_currency = (watch.target_currency or item.currency or "JPY").upper()
    display_currency = (user.display_currency or settings.display_currency).upper()

    price, variant = effective_price(item, watch)

    breakdown = landed_cost.estimate(
        db,
        price,
        item.currency,
        profile,
        target_currency=display_currency,
        item=item,
    )
    landed_total = breakdown.total if breakdown else None

    if watch.price_basis == PriceBasis.landed:
        if target_currency == display_currency:
            compare = landed_total
        else:
            other = landed_cost.estimate(
                db,
                price,
                item.currency,
                profile,
                target_currency=target_currency,
                item=item,
            )
            compare = other.total if other else None
    else:
        compare = fx.convert(db, price, item.currency, target_currency)

    return Valuation(
        compare_price=compare,
        compare_currency=target_currency,
        landed_total=landed_total,
        landed_currency=display_currency,
        breakdown=breakdown.as_dict() if breakdown else None,
        shop_price=price,
        variant=variant,
    )


def decide_trigger(
    watch: Watch,
    item: Item,
    seen: WatchSeenItem | None,
    valuation: Valuation,
) -> TriggerType | None:
    """Pick the single most important reason to alert, or None to stay quiet."""
    candidates: set[TriggerType] = set()
    target = watch.target_price
    price = valuation.compare_price

    if (watch.min_item_grade or watch.min_box_grade) and valuation.shop_price is None:
        # No listing under this product code is good enough, or the graded
        # listings are not known yet. Either way there is nothing to announce.
        return None

    # These columns are NOT NULL with a default of True, but a Watch that has
    # not been flushed yet still carries None, so read them explicitly.
    on_price_below = watch.notify_on_price_below is not False
    on_restock = watch.notify_on_restock is not False
    on_new_match = watch.notify_on_new_match is not False

    hit_target = on_price_below and target is not None and price is not None and price <= target
    if hit_target:
        # Fire on the crossing, not on every poll while the price sits below
        # the target. An item that was already cheap when the watch was
        # created is not news; one that just came down across the line is.
        if seen is None:
            # A listing that did not exist last time and is already under the
            # target is genuinely new information.
            candidates.add(TriggerType.price_below)
        elif seen.last_compare_price is not None and seen.last_compare_price > target:
            # It came down across the line since the last poll.
            candidates.add(TriggerType.price_below)

    if on_restock and item.in_stock:
        if seen is not None and not seen.last_in_stock:
            candidates.add(
                TriggerType.restock_preowned
                if item.condition == Condition.preowned
                else TriggerType.back_in_stock
            )

    drop_pct = watch.notify_on_price_drop_pct
    observed = valuation.shop_price if valuation.shop_price is not None else item.current_price
    if drop_pct and seen is not None and seen.last_price and observed and seen.last_price > 0:
        drop = 1.0 - (observed / seen.last_price)
        if drop * 100.0 >= drop_pct:
            candidates.add(TriggerType.price_drop)

    if on_new_match and seen is None:
        # A brand new listing only counts when it is actually buyable, and
        # when it satisfies the target price if one is set.
        buyable = item.in_stock or item.is_preorder or item.is_backorder
        if buyable and (target is None or hit_target):
            candidates.add(TriggerType.new_match)

    for trigger in TRIGGER_PRIORITY:
        if trigger in candidates:
            return trigger
    return None


def _cooled_down(watch: Watch, seen: WatchSeenItem | None, trigger: TriggerType) -> bool:
    if seen is None or seen.last_alert_at is None:
        return True
    elapsed = datetime.now(timezone.utc) - _aware(seen.last_alert_at)
    if seen.last_trigger != trigger.value:
        # A different kind of news gets through on a shorter leash.
        return elapsed >= timedelta(seconds=max(60, watch.cooldown_seconds // 4))
    return elapsed >= timedelta(seconds=watch.cooldown_seconds)


def _alerts_today(db: Session, watch: Watch) -> int:
    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        db.execute(
            select(func.count(Alert.id)).where(
                Alert.watch_id == watch.id, Alert.created_at >= midnight
            )
        ).scalar_one()
        or 0
    )


def _alert_title(trigger: TriggerType, item: Item) -> str:
    label = notify.TRIGGER_LABELS.get(trigger, "Update")
    name = item.name if len(item.name) <= 90 else item.name[:87] + "..."
    return f"{label}: {name}"


def _build_alert(
    watch: Watch,
    item: Item,
    trigger: TriggerType,
    valuation: Valuation,
    seen: WatchSeenItem | None,
    shop_name: str,
) -> Alert:
    extra = {
        "item_name": item.name,
        "shop": shop_name,
        "condition": "Pre-owned" if item.condition == Condition.preowned else "New",
        "watch_label": watch.label or watch.query,
        "breakdown": valuation.breakdown,
    }
    if valuation.variant:
        # Name the exact listing, because on a multi-grade product the price
        # only means something together with the condition it applies to.
        grades = " / ".join(
            part
            for part in (
                f"Item:{valuation.variant.get('item_grade')}"
                if valuation.variant.get("item_grade")
                else "",
                f"Box:{valuation.variant.get('box_grade')}"
                if valuation.variant.get("box_grade")
                else "",
            )
            if part
        )
        extra["grade"] = grades or valuation.variant.get("condition") or ""
        extra["listing"] = valuation.variant.get("code")
    elif item.condition_grade:
        extra["grade"] = item.condition_grade

    if item.price_max and item.current_price and item.price_max > item.current_price:
        extra["price_range"] = (
            f"{item.current_price:,.0f} to {item.price_max:,.0f} {item.currency} "
            "across the graded copies on offer"
        )
    if item.release_date:
        extra["release"] = item.release_date
    if item.in_stock:
        extra["stock"] = "In stock"
    elif item.is_preorder:
        extra["stock"] = "Pre-order"
    elif item.is_backorder:
        extra["stock"] = "Back-order"

    body = ""
    if watch.target_price is not None and valuation.compare_price is not None:
        basis = "total incl. import" if watch.price_basis == PriceBasis.landed else "shop price"
        body = (
            f"Your target was {notify.format_money(watch.target_price, valuation.compare_currency)} "
            f"({basis})."
        )

    urgency = _urgency_note(item)
    if urgency:
        # The whole point of this application is arriving in time to act. A
        # price alert for something that sits for a month and one for
        # something gone by tomorrow deserve different reactions, and only we
        # know which is which.
        extra["urgency"] = urgency
        body = f"{body} {urgency}".strip()

    return Alert(
        user_id=watch.user_id,
        watch_id=watch.id,
        item_id=item.id,
        trigger=trigger,
        title=_alert_title(trigger, item),
        body=body,
        price=valuation.shop_price if valuation.shop_price is not None else item.current_price,
        currency=item.currency,
        landed_price=valuation.landed_total,
        landed_currency=valuation.landed_currency,
        previous_price=seen.last_price if seen else None,
        url=item.product_url,
        image_url=item.image_url,
        extra=extra,
    )


def _urgency_note(item: Item) -> str:
    """One line on how fast copies of this figure disappear.

    Deliberately says where the number came from. A figure measured from
    copies we watched sell is worth acting on; one extrapolated from the
    intake counter is a hint, and should not read like a countdown.
    """
    days = item.dwell_days
    if not days:
        return ""

    if days < 1:
        sells, lasts = "within a day", "less than a day"
    elif days < 2:
        sells, lasts = "in about a day", "about a day"
    else:
        sells, lasts = f"in about {days:.0f} days", f"about {days:.0f} days"

    if item.dwell_basis == "observed":
        samples = item.dwell_samples or 0
        return f"Copies of this figure sell {sells} ({samples} watched here)."
    if item.dwell_basis == "intake":
        return f"Going by shop turnover, copies last {lasts}."
    if item.dwell_basis == "intake_bootstrap":
        return f"Rough estimate from shop turnover: copies last {lasts}."
    if item.dwell_basis == "product":
        return f"This listing stood {lasts} last time before selling out."
    return ""


def _remember(
    db: Session,
    watch: Watch,
    item: Item,
    alerted: TriggerType | None,
    compare_price: float | None = None,
    shop_price: float | None = None,
) -> WatchSeenItem:
    seen = db.execute(
        select(WatchSeenItem).where(
            WatchSeenItem.watch_id == watch.id, WatchSeenItem.item_id == item.id
        )
    ).scalar_one_or_none()
    if seen is None:
        seen = WatchSeenItem(watch_id=watch.id, item_id=item.id)
        db.add(seen)
    seen.last_seen_at = utcnow()
    seen.last_price = shop_price if shop_price is not None else item.current_price
    if compare_price is not None:
        seen.last_compare_price = compare_price
    seen.last_in_stock = item.in_stock
    if alerted is not None:
        seen.last_alert_at = utcnow()
        seen.last_trigger = alerted.value
    return seen


def next_interval(watch: Watch, outcome: RunOutcome, had_hot_signal: bool) -> int:
    """How long to wait before polling this watch again, in seconds.

    Manual intervals are respected exactly (down to the global floor). Adaptive
    watches speed up when they look close to firing and slow down when nothing
    has moved, so the request budget is spent where it can actually win a
    listing.
    """
    floor = settings.min_poll_interval_seconds
    base = watch.interval_seconds or settings.default_poll_interval_seconds
    # Column defaults only apply on insert, so an object that has not been
    # flushed yet can still carry None here.
    priority = watch.priority or 0
    errors = watch.consecutive_errors or 0

    if not watch.adaptive or not settings.adaptive_polling:
        return max(floor, base)

    interval = float(base)
    if had_hot_signal:
        # Something is close to triggering: poll near the floor.
        interval = max(floor, base * 0.25)
    elif outcome.alerts:
        interval = max(floor, base * 0.5)
    elif errors:
        # Exponential backoff, capped at an hour, so a broken watch stops
        # eating the shared rate limit.
        interval = min(3600.0, base * (2 ** min(errors, 5)))

    if priority > 0:
        interval = max(floor, interval / (1 + priority))

    # Jitter prevents every watch created in one sitting from firing together.
    interval *= random.uniform(0.9, 1.1)
    return int(max(floor, min(interval, 86400)))


def _is_hot(watch: Watch, valuations: list[Valuation]) -> bool:
    """True when at least one match is within 15% of the target price."""
    if watch.target_price is None:
        return False
    for valuation in valuations:
        if valuation.compare_price is None:
            continue
        if valuation.compare_price <= watch.target_price * 1.15:
            return True
    return False


def _result_hash(items: list[Item]) -> str:
    payload = "|".join(f"{i.code}:{i.current_price}:{int(i.in_stock)}" for i in items)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _summary_alert(
    db: Session,
    watch: Watch,
    user: User,
    shop_name: str,
    items: list[Item],
    title: str,
) -> Alert:
    """One alert standing in for many matches."""
    cheapest = min(
        (i for i in items if i.current_price is not None),
        key=lambda i: i.current_price,
        default=None,
    )
    lines = []
    for item in items[:10]:
        price = notify.format_money(item.current_price, item.currency) or "price unknown"
        lines.append(f"- {item.name[:70]} ... {price}")
    if len(items) > 10:
        lines.append(f"...and {len(items) - 10} more")

    valuation = value_item(db, cheapest, watch, user) if cheapest else None
    alert = Alert(
        user_id=watch.user_id,
        watch_id=watch.id,
        item_id=cheapest.id if cheapest else None,
        trigger=TriggerType.new_match,
        title=title,
        body=NEWLINE.join(lines),
        price=cheapest.current_price if cheapest else None,
        currency=cheapest.currency if cheapest else "JPY",
        landed_price=valuation.landed_total if valuation else None,
        landed_currency=valuation.landed_currency if valuation else "EUR",
        url=cheapest.product_url if cheapest else None,
        image_url=cheapest.image_url if cheapest else None,
        extra={
            "item_name": cheapest.name if cheapest else None,
            "shop": shop_name,
            "summary": True,
            "match_count": len(items),
            "watch_label": watch.label or watch.query,
        },
    )
    db.add(alert)
    db.commit()
    notify.deliver(db, alert, user, watch=watch, shop_name=shop_name)
    return alert


def _baseline(
    db: Session,
    watch: Watch,
    items: list[Item],
    user: User,
    shop_name: str,
    outcome: RunOutcome,
    started: float,
) -> RunOutcome:
    """Record the starting state of a watch without alerting on it."""
    import time as _time

    for item in items:
        valuation = value_item(db, item, watch, user)
        _remember(
            db,
            watch,
            item,
            None,
            compare_price=valuation.compare_price,
            shop_price=valuation.shop_price,
        )
    watch.baselined = True
    watch.last_result_hash = _result_hash(items)
    outcome.matched = len(items)

    if items:
        _summary_alert(
            db,
            watch,
            user,
            shop_name,
            items,
            title=f"Watching: {len(items)} listing(s) match right now",
        )
        outcome.alerts = 1
        watch.alert_count += 1

    outcome.took_ms = int((_time.monotonic() - started) * 1000)
    watch.next_run_at = datetime.now(timezone.utc) + timedelta(
        seconds=next_interval(watch, outcome, False)
    )
    outcome.next_run_at = watch.next_run_at
    db.commit()
    log.info(
        "Watch %s baselined with %s existing listing(s); alerts start from the next change",
        watch.id,
        len(items),
    )
    return outcome


def _collect_items(db: Session, watch: Watch) -> tuple[list[Item], str]:
    """Fetch the current candidate set for a watch. Returns (items, shop_name)."""
    provider = get_provider(watch.provider)

    if watch.kind == WatchKind.item:
        code = watch.item_code or ""
        existing = catalog.get_item(db, watch.provider, code)
        try:
            normalized = provider.get_item(code)
        except ItemNotFound:
            # AmiAmi deletes sold-out pre-owned listings outright, so this is
            # the shop telling us the item is gone, not a failure.
            if existing is not None:
                catalog.mark_unavailable(db, existing)
                return [existing], provider.name
            raise
        item, _ = catalog.upsert_item(db, normalized)
        return [item], provider.name

    result = provider.search(build_query(watch))
    items: list[Item] = []
    for normalized in result.items:
        item, _ = catalog.upsert_item(db, normalized, commit=False)
        items.append(item)
    db.commit()

    if watch.min_item_grade or watch.min_box_grade:
        _resolve_grades(db, provider, watch, items)
    return items, provider.name


def _resolve_grades(db: Session, provider, watch: Watch, items: list[Item]) -> None:
    """Fetch the graded sub-listings for candidates a grade filter needs.

    Search responses only carry a price range, never the individual grades, so
    a watch filtering on condition has to open the detail page. That is one
    extra request per item, which would be wasteful across a whole result
    page, so it is spent only on the items that could plausibly match: the
    ones already within reach of the target price, cheapest first, capped per
    run. The rest keep their existing data and get resolved on a later poll.
    """
    ceiling = watch.target_price
    candidates = [
        item
        for item in items
        if item.current_price is not None and not (item.variants or [])
    ]
    # Without a target every candidate is equally plausible, so just take the
    # cheapest ones; with one, anything above it cannot qualify anyway since
    # grades only ever cost more.
    if ceiling is not None and watch.price_basis == PriceBasis.listed:
        candidates = [i for i in candidates if (i.current_price or 0) <= ceiling * 2]
    candidates.sort(key=lambda i: i.current_price or 0)

    for item in candidates[:MAX_GRADE_LOOKUPS_PER_RUN]:
        try:
            detailed = provider.get_item(item.code)
        except ItemNotFound:
            catalog.mark_unavailable(db, item)
            continue
        except ProviderError as exc:
            log.debug("Could not resolve grades for %s: %s", item.code, exc)
            break
        catalog.upsert_item(db, detailed, commit=False)
    db.commit()


def run_watch(db: Session, watch: Watch) -> RunOutcome:
    """Poll one watch, raise alerts, and reschedule it."""
    import time as _time

    started = _time.monotonic()
    outcome = RunOutcome(watch_id=watch.id)
    watch.last_run_at = utcnow()
    watch.run_count += 1

    user = db.get(User, watch.user_id)
    if user is None or not user.is_active:
        watch.enabled = False
        db.commit()
        outcome.error = "Owner account is inactive"
        return outcome

    try:
        items, shop_name = _collect_items(db, watch)
    except (ProviderError, ItemNotFound) as exc:
        watch.consecutive_errors += 1
        watch.last_error = str(exc)[:500]
        outcome.error = str(exc)
        outcome.took_ms = int((_time.monotonic() - started) * 1000)
        watch.next_run_at = datetime.now(timezone.utc) + timedelta(
            seconds=next_interval(watch, outcome, False)
        )
        outcome.next_run_at = watch.next_run_at
        db.commit()
        return outcome

    watch.consecutive_errors = 0
    watch.last_error = None
    watch.last_success_at = utcnow()
    outcome.checked = len(items)

    fx.ensure_fresh(db)

    # First contact with a query: record the current world silently. A broad
    # search can match dozens of listings, and firing one alert per listing the
    # moment the watch is created is exactly the noise this app exists to fix.
    if not watch.baselined:
        return _baseline(db, watch, items, user, shop_name, outcome, started)

    budget_left = max(0, watch.max_alerts_per_day - _alerts_today(db, watch))
    valuations: list[Valuation] = []
    deferred: list[tuple[Item, TriggerType, Valuation]] = []

    for item in items:
        valuation = value_item(db, item, watch, user)
        valuations.append(valuation)

        seen = db.execute(
            select(WatchSeenItem).where(
                WatchSeenItem.watch_id == watch.id, WatchSeenItem.item_id == item.id
            )
        ).scalar_one_or_none()

        trigger = decide_trigger(watch, item, seen, valuation)
        fired: TriggerType | None = None

        if trigger is not None:
            outcome.matched += 1
            if budget_left <= 0:
                log.info("Watch %s hit its daily alert cap", watch.id)
            elif not _cooled_down(watch, seen, trigger):
                log.debug("Watch %s: %s still cooling down", watch.id, item.code)
            elif outcome.alerts >= MAX_ALERTS_PER_RUN:
                # Roll the tail into one summary instead of a burst of pushes.
                deferred.append((item, trigger, valuation))
            else:
                alert = _build_alert(watch, item, trigger, valuation, seen, shop_name)
                db.add(alert)
                db.commit()
                notify.deliver(db, alert, user, watch=watch, shop_name=shop_name)
                fired = trigger
                budget_left -= 1
                outcome.alerts += 1
                outcome.triggers.append(trigger.value)
                watch.alert_count += 1

        _remember(
            db,
            watch,
            item,
            fired,
            compare_price=valuation.compare_price,
            shop_price=valuation.shop_price,
        )

    if deferred and budget_left > 0:
        _summary_alert(
            db,
            watch,
            user,
            shop_name,
            [entry[0] for entry in deferred],
            title=f"{len(deferred)} more match(es) for {watch.label or watch.query}",
        )
        outcome.alerts += 1
        for item, trigger, deferred_valuation in deferred:
            _remember(
                db,
                watch,
                item,
                trigger,
                compare_price=deferred_valuation.compare_price,
                shop_price=deferred_valuation.shop_price,
            )

    watch.last_result_hash = _result_hash(items)
    outcome.took_ms = int((_time.monotonic() - started) * 1000)
    watch.next_run_at = datetime.now(timezone.utc) + timedelta(
        seconds=next_interval(watch, outcome, _is_hot(watch, valuations))
    )
    outcome.next_run_at = watch.next_run_at
    db.commit()

    log.info(
        "Watch %s (%s): %s checked, %s matched, %s alerts in %sms, next in %ss",
        watch.id,
        watch.label or watch.query or watch.item_code,
        outcome.checked,
        outcome.matched,
        outcome.alerts,
        outcome.took_ms,
        int((watch.next_run_at - datetime.now(timezone.utc)).total_seconds()),
    )
    return outcome
