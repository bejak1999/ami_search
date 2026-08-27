"""Alert feed, read state and the live SSE stream."""
from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import Text, delete, func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..events import bus, format_sse
from ..models import Alert, TriggerType, User, utcnow
from ..schemas import AlertOut, MessageResponse
from .serializers import alert_out

router = APIRouter(prefix="/alerts", tags=["alerts"])

HEARTBEAT_SECONDS = 25


@router.get("", response_model=list[AlertOut])
def list_alerts(
    unread_only: bool = False,
    trigger: TriggerType | None = None,
    watch_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[AlertOut]:
    stmt = select(Alert).where(Alert.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Alert.read_at.is_(None))
    if trigger is not None:
        # An alert can qualify several ways at once and is named after the
        # most important. Filtering only on that name hid alerts that really
        # did meet the reason being asked for, so the stored list is searched
        # as well. LIKE on a JSON array is crude but portable, and the quotes
        # stop "restock" matching "restock_preowned".
        needle = f'"{trigger.value}"'
        stmt = stmt.where(
            or_(Alert.trigger == trigger, Alert.reasons.cast(Text).like(f"%{needle}%"))
        )
    if watch_id is not None:
        stmt = stmt.where(Alert.watch_id == watch_id)
    rows = db.execute(
        stmt.order_by(Alert.created_at.desc()).offset(offset).limit(limit)
    ).scalars()
    return [alert_out(db, a) for a in rows]


@router.get("/unread-count", response_model=MessageResponse)
def unread_count(db: Session = Depends(get_db), user: User = Depends(current_user)) -> MessageResponse:
    count = int(
        db.execute(
            select(func.count(Alert.id)).where(
                Alert.user_id == user.id, Alert.read_at.is_(None)
            )
        ).scalar_one()
        or 0
    )
    return MessageResponse(message=str(count), detail={"count": count})


@router.post("/{alert_id}/read", response_model=AlertOut)
def mark_read(
    alert_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> AlertOut:
    alert = db.get(Alert, alert_id)
    if alert is None or alert.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")
    if alert.read_at is None:
        alert.read_at = utcnow()
        db.commit()
    return alert_out(db, alert)


@router.post("/read-all", response_model=MessageResponse)
def mark_all_read(db: Session = Depends(get_db), user: User = Depends(current_user)) -> MessageResponse:
    rows = db.execute(
        select(Alert).where(Alert.user_id == user.id, Alert.read_at.is_(None))
    ).scalars()
    count = 0
    for alert in rows:
        alert.read_at = utcnow()
        count += 1
    db.commit()
    return MessageResponse(message=f"Marked {count} alert(s) as read")


@router.delete("", response_model=MessageResponse)
def clear_alerts(
    read_only: bool = True, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> MessageResponse:
    stmt = delete(Alert).where(Alert.user_id == user.id)
    if read_only:
        stmt = stmt.where(Alert.read_at.is_not(None))
    result = db.execute(stmt)
    db.commit()
    return MessageResponse(message=f"Deleted {int(result.rowcount or 0)} alert(s)")


@router.get("/stream")
async def stream(request: Request, user: User = Depends(current_user)) -> StreamingResponse:
    """Server-sent events: new alerts appear in the UI without polling."""
    subscriber = bus.subscribe(user.id)

    async def generator():
        try:
            yield format_sse({"event": "ready", "data": {"user_id": user.id}})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(
                        subscriber.queue.get(), timeout=HEARTBEAT_SECONDS
                    )
                except asyncio.TimeoutError:
                    # Comment frames keep proxies from closing an idle stream.
                    yield ": keepalive\n\n"
                    continue
                yield format_sse(payload)
        finally:
            bus.unsubscribe(subscriber)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/stats", response_model=MessageResponse)
def alert_stats(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> MessageResponse:
    """Daily alert counts grouped by trigger, for the dashboard chart."""
    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(
            func.date(Alert.created_at).label("day"),
            Alert.trigger,
            func.count(Alert.id),
        )
        .where(Alert.user_id == user.id, Alert.created_at >= cutoff)
        .group_by("day", Alert.trigger)
        .order_by("day")
    ).all()

    series: dict[str, dict[str, int]] = {}
    for day, trigger, count in rows:
        key = str(day)
        series.setdefault(key, {})[trigger.value] = int(count)

    return MessageResponse(
        message="ok",
        detail={
            "days": days,
            "series": [{"day": day, **counts} for day, counts in sorted(series.items())],
        },
    )
