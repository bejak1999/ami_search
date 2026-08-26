"""Saved searches and item watches."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import current_user, user_cost_profile
from ..models import (
    Alert,
    CostProfile,
    Item,
    User,
    Watch,
    WatchKind,
    WatchSeenItem,
)
from ..providers import detect_provider_from_url, get_provider, provider_ids
from ..schemas import MessageResponse, WatchCreate, WatchOut, WatchUpdate
from ..services import matcher
from .serializers import item_out

router = APIRouter(prefix="/watches", tags=["watches"])


def _get_or_404(db: Session, watch_id: int, user: User) -> Watch:
    watch = db.get(Watch, watch_id)
    if watch is None or watch.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watch not found")
    return watch


def _serialize(
    db: Session,
    watch: Watch,
    user: User,
    profile: CostProfile,
    with_items: bool = False,
) -> WatchOut:
    payload = WatchOut.model_validate(watch)
    payload.effective_interval_seconds = max(
        settings.min_poll_interval_seconds,
        watch.interval_seconds or settings.default_poll_interval_seconds,
    )
    payload.match_count = int(
        db.execute(
            select(func.count(WatchSeenItem.id)).where(WatchSeenItem.watch_id == watch.id)
        ).scalar_one()
        or 0
    )
    if with_items:
        rows = db.execute(
            select(Item)
            .join(WatchSeenItem, WatchSeenItem.item_id == Item.id)
            .where(WatchSeenItem.watch_id == watch.id)
            .order_by(WatchSeenItem.last_seen_at.desc())
            .limit(12)
        ).scalars()
        payload.recent_items = [
            item_out(db, item, user=user, profile=profile) for item in rows
        ]
    return payload


@router.get("", response_model=list[WatchOut])
def list_watches(
    enabled: bool | None = None,
    with_items: bool = Query(default=True),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> list[WatchOut]:
    stmt = select(Watch).where(Watch.user_id == user.id)
    if enabled is not None:
        stmt = stmt.where(Watch.enabled.is_(enabled))
    rows = db.execute(stmt.order_by(Watch.priority.desc(), Watch.created_at.desc())).scalars()
    return [_serialize(db, w, user, profile, with_items=with_items) for w in rows]


@router.post("", response_model=WatchOut, status_code=status.HTTP_201_CREATED)
def create_watch(
    payload: WatchCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> WatchOut:
    if payload.provider not in provider_ids():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown shop: " + payload.provider
        )

    item_code = payload.item_code
    if payload.kind == WatchKind.item:
        if not item_code:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An item watch needs a product code or shop link",
            )
        # Accept a pasted URL just as readily as a bare code.
        detected = detect_provider_from_url(item_code)
        if detected:
            _, item_code = detected
    elif not payload.query.strip() and not payload.filters:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A search watch needs either search terms or at least one filter",
        )

    label = payload.label.strip()
    if not label:
        label = item_code if payload.kind == WatchKind.item else payload.query.strip()

    watch = Watch(
        user_id=user.id,
        provider=payload.provider,
        kind=payload.kind,
        label=label[:200],
        query=payload.query.strip(),
        item_code=item_code,
        filters=payload.filters,
        condition=payload.condition,
        stock_filter=payload.stock_filter,
        target_price=payload.target_price,
        price_basis=payload.price_basis,
        target_currency=payload.target_currency.upper(),
        notify_on_price_below=payload.notify_on_price_below,
        notify_on_restock=payload.notify_on_restock,
        notify_on_new_match=payload.notify_on_new_match,
        notify_on_price_drop_pct=payload.notify_on_price_drop_pct,
        enabled=payload.enabled,
        interval_seconds=payload.interval_seconds,
        adaptive=payload.adaptive,
        priority=payload.priority,
        quiet_hours=payload.quiet_hours.model_dump(),
        cooldown_seconds=payload.cooldown_seconds,
        max_alerts_per_day=payload.max_alerts_per_day,
        channel_ids=payload.channel_ids,
    )
    db.add(watch)
    db.commit()
    db.refresh(watch)
    return _serialize(db, watch, user, profile)


@router.get("/{watch_id}", response_model=WatchOut)
def get_watch(
    watch_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> WatchOut:
    return _serialize(db, _get_or_404(db, watch_id, user), user, profile, with_items=True)


@router.patch("/{watch_id}", response_model=WatchOut)
def update_watch(
    watch_id: int,
    payload: WatchUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> WatchOut:
    watch = _get_or_404(db, watch_id, user)
    data = payload.model_dump(exclude_unset=True)

    if "quiet_hours" in data and data["quiet_hours"] is not None:
        watch.quiet_hours = data.pop("quiet_hours")
    if data.get("target_currency"):
        data["target_currency"] = data["target_currency"].upper()

    for key, value in data.items():
        setattr(watch, key, value)

    # Any settings change should take effect on the next tick, not after the
    # old interval finally elapses.
    watch.next_run_at = None
    db.commit()
    db.refresh(watch)
    return _serialize(db, watch, user, profile, with_items=True)


@router.delete("/{watch_id}", response_model=MessageResponse)
def delete_watch(
    watch_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> MessageResponse:
    watch = _get_or_404(db, watch_id, user)
    db.delete(watch)
    db.commit()
    return MessageResponse(message="Watch deleted")


@router.post("/{watch_id}/run", response_model=MessageResponse)
def run_watch_now(
    watch_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> MessageResponse:
    """Check this watch immediately, without waiting for the scheduler."""
    watch = _get_or_404(db, watch_id, user)
    outcome = matcher.run_watch(db, watch)
    if outcome.error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=outcome.error)
    return MessageResponse(
        message=(
            f"Checked {outcome.checked} listing(s), {outcome.matched} matched, "
            f"{outcome.alerts} alert(s) sent"
        ),
        detail={
            "checked": outcome.checked,
            "matched": outcome.matched,
            "alerts": outcome.alerts,
            "took_ms": outcome.took_ms,
            "next_run_at": outcome.next_run_at,
            "triggers": outcome.triggers,
        },
    )


@router.post("/{watch_id}/preview", response_model=MessageResponse)
def preview_watch(
    watch_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> MessageResponse:
    """Run the watch's query without touching alert state.

    Lets someone tune filters and a target price and immediately see what
    would have matched, which is the fastest way to spot a query that is far
    too broad.
    """
    watch = _get_or_404(db, watch_id, user)
    provider = get_provider(watch.provider)
    results = provider.search(matcher.build_query(watch))

    preview = []
    for normalized in results.items[:24]:
        stored = db.execute(
            select(Item).where(Item.provider == normalized.provider, Item.code == normalized.code)
        ).scalar_one_or_none()
        if stored is None:
            continue
        payload = item_out(db, stored, user=user, profile=profile)
        valuation = matcher.value_item(db, stored, watch, user)
        preview.append(
            {
                "item": payload.model_dump(mode="json"),
                "compare_price": valuation.compare_price,
                "compare_currency": valuation.compare_currency,
                "would_match": (
                    watch.target_price is None
                    or (
                        valuation.compare_price is not None
                        and valuation.compare_price <= watch.target_price
                    )
                ),
            }
        )

    return MessageResponse(
        message=f"{results.total} total result(s) upstream",
        detail={"total": results.total, "preview": preview},
    )


@router.get("/{watch_id}/alerts", response_model=list[dict])
def watch_alerts(
    watch_id: int,
    limit: int = Query(default=30, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    _get_or_404(db, watch_id, user)
    rows = db.execute(
        select(Alert)
        .where(Alert.watch_id == watch_id)
        .order_by(Alert.created_at.desc())
        .limit(limit)
    ).scalars()
    from .serializers import alert_out

    return [alert_out(db, a).model_dump(mode="json") for a in rows]
