"""Wishlist and collection."""
from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, user_cost_profile
from ..models import CollectionEntry, CollectionStatus, CostProfile, Item, User, utcnow
from ..providers import ItemNotFound, ProviderError, detect_provider_from_url, get_provider
from ..schemas import CollectionCreate, CollectionOut, CollectionUpdate, MessageResponse
from ..services import catalog, fx, landed_cost
from .serializers import item_out

router = APIRouter(prefix="/collection", tags=["collection"])


#: Scalar columns copied straight across. The ``item`` relationship is built
#: separately because ItemOut renames and enriches the ORM columns.
_ENTRY_FIELDS = (
    "id",
    "status",
    "priority",
    "notes",
    "tags",
    "paid_price",
    "paid_currency",
    "purchased_at",
    "quantity",
    "mfc_url",
    "created_at",
    "updated_at",
)


def _serialize(
    db: Session, entry: CollectionEntry, user: User, profile: CostProfile
) -> CollectionOut:
    return CollectionOut(
        **{name: getattr(entry, name) for name in _ENTRY_FIELDS},
        item=item_out(db, entry.item, user=user, profile=profile, with_context=True),
    )


def _resolve_item(db: Session, payload: CollectionCreate) -> Item:
    if payload.item_id:
        item = db.get(Item, payload.item_id)
        if item is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
        return item

    if not payload.item_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Provide an item id, code or link"
        )

    provider_id, code = payload.provider, payload.item_code
    detected = detect_provider_from_url(payload.item_code)
    if detected:
        provider_id, code = detected

    existing = catalog.get_item(db, provider_id, code)
    if existing is not None:
        return existing

    try:
        normalized = get_provider(provider_id).get_item(code)
    except ItemNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    item, _ = catalog.upsert_item(db, normalized)
    return item


@router.get("", response_model=list[CollectionOut])
def list_entries(
    status_filter: CollectionStatus | None = Query(default=None, alias="status"),
    tag: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> list[CollectionOut]:
    stmt = select(CollectionEntry).where(CollectionEntry.user_id == user.id)
    if status_filter is not None:
        stmt = stmt.where(CollectionEntry.status == status_filter)
    rows = list(
        db.execute(
            stmt.order_by(CollectionEntry.priority, CollectionEntry.updated_at.desc())
        ).scalars()
    )
    if tag:
        rows = [r for r in rows if tag in (r.tags or [])]
    return [_serialize(db, entry, user, profile) for entry in rows]


@router.post("", response_model=CollectionOut, status_code=status.HTTP_201_CREATED)
def add_entry(
    payload: CollectionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> CollectionOut:
    item = _resolve_item(db, payload)

    entry = db.execute(
        select(CollectionEntry).where(
            CollectionEntry.user_id == user.id, CollectionEntry.item_id == item.id
        )
    ).scalar_one_or_none()
    if entry is None:
        entry = CollectionEntry(user_id=user.id, item_id=item.id)
        db.add(entry)

    entry.status = payload.status
    entry.priority = payload.priority
    entry.notes = payload.notes
    entry.tags = payload.tags
    entry.paid_price = payload.paid_price
    entry.paid_currency = payload.paid_currency.upper()
    entry.quantity = payload.quantity
    entry.mfc_url = payload.mfc_url
    if payload.status in (CollectionStatus.owned, CollectionStatus.ordered) and entry.purchased_at is None:
        entry.purchased_at = utcnow()
    db.commit()
    db.refresh(entry)
    return _serialize(db, entry, user, profile)


@router.patch("/{entry_id}", response_model=CollectionOut)
def update_entry(
    entry_id: int,
    payload: CollectionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> CollectionOut:
    entry = db.get(CollectionEntry, entry_id)
    if entry is None or entry.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")

    data = payload.model_dump(exclude_unset=True)
    if data.get("paid_currency"):
        data["paid_currency"] = data["paid_currency"].upper()
    for key, value in data.items():
        setattr(entry, key, value)
    if entry.status == CollectionStatus.owned and entry.purchased_at is None:
        entry.purchased_at = utcnow()
    db.commit()
    db.refresh(entry)
    return _serialize(db, entry, user, profile)


@router.delete("/{entry_id}", response_model=MessageResponse)
def delete_entry(
    entry_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> MessageResponse:
    entry = db.get(CollectionEntry, entry_id)
    if entry is None or entry.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")
    db.delete(entry)
    db.commit()
    return MessageResponse(message="Removed")


@router.get("/summary", response_model=MessageResponse)
def summary(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> MessageResponse:
    """What the collection is worth, and what the wishlist would cost today."""
    display = (user.display_currency or "EUR").upper()
    entries = list(
        db.execute(
            select(CollectionEntry).where(CollectionEntry.user_id == user.id)
        ).scalars()
    )

    counts: dict[str, int] = {}
    spent = 0.0
    market_value = 0.0
    wishlist_landed = 0.0

    for entry in entries:
        counts[entry.status.value] = counts.get(entry.status.value, 0) + 1
        item = entry.item

        if entry.paid_price:
            converted = fx.convert(db, entry.paid_price, entry.paid_currency, display)
            spent += (converted or 0.0) * entry.quantity

        if entry.status == CollectionStatus.owned and item.current_price:
            converted = fx.convert(db, item.current_price, item.currency, display)
            market_value += (converted or 0.0) * entry.quantity

        if entry.status == CollectionStatus.wishlist:
            breakdown = landed_cost.estimate(
                db,
                item.current_price,
                item.currency,
                profile,
                target_currency=display,
                item=item,
                quantity=entry.quantity,
            )
            if breakdown:
                wishlist_landed += breakdown.total

    return MessageResponse(
        message="ok",
        detail={
            "currency": display,
            "counts": counts,
            "total_entries": len(entries),
            "spent": round(spent, 2),
            "market_value": round(market_value, 2),
            "unrealized": round(market_value - spent, 2),
            "wishlist_landed_total": round(wishlist_landed, 2),
        },
    )


@router.get("/export")
def export_collection(
    fmt: str = Query(default="json", pattern="^(json|csv)$"),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    rows = list(
        db.execute(
            select(CollectionEntry).where(CollectionEntry.user_id == user.id)
        ).scalars()
    )
    records = [
        {
            "provider": entry.item.provider,
            "code": entry.item.code,
            "name": entry.item.name,
            "status": entry.status.value,
            "priority": entry.priority,
            "quantity": entry.quantity,
            "paid_price": entry.paid_price,
            "paid_currency": entry.paid_currency,
            "purchased_at": entry.purchased_at.isoformat() if entry.purchased_at else "",
            "tags": ",".join(entry.tags or []),
            "notes": entry.notes,
            "mfc_url": entry.mfc_url or "",
            "url": entry.item.product_url or "",
        }
        for entry in rows
    ]

    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(records[0]) if records else ["code"])
        writer.writeheader()
        writer.writerows(records)
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="amisearch-collection.csv"'},
        )

    return Response(
        content=json.dumps(records, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="amisearch-collection.json"'},
    )


@router.post("/import", response_model=MessageResponse)
def import_collection(
    payload: dict, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> MessageResponse:
    """Import previously exported entries. Existing rows are updated, not duplicated."""
    records = payload.get("records")
    if not isinstance(records, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Expected a 'records' array"
        )

    imported, skipped = 0, 0
    for record in records[:500]:
        code = str(record.get("code") or "").strip()
        if not code:
            skipped += 1
            continue
        provider_id = str(record.get("provider") or "amiami")
        item = catalog.get_item(db, provider_id, code)
        if item is None:
            try:
                normalized = get_provider(provider_id).get_item(code)
            except (ItemNotFound, ProviderError, KeyError):
                skipped += 1
                continue
            item, _ = catalog.upsert_item(db, normalized)

        entry = db.execute(
            select(CollectionEntry).where(
                CollectionEntry.user_id == user.id, CollectionEntry.item_id == item.id
            )
        ).scalar_one_or_none()
        if entry is None:
            entry = CollectionEntry(user_id=user.id, item_id=item.id)
            db.add(entry)

        try:
            entry.status = CollectionStatus(record.get("status") or "wishlist")
        except ValueError:
            entry.status = CollectionStatus.wishlist
        entry.priority = int(record.get("priority") or 2)
        entry.quantity = int(record.get("quantity") or 1)
        entry.notes = str(record.get("notes") or "")
        tags = record.get("tags")
        entry.tags = tags.split(",") if isinstance(tags, str) and tags else (tags or [])
        entry.paid_price = record.get("paid_price")
        entry.paid_currency = str(record.get("paid_currency") or "JPY").upper()
        entry.mfc_url = record.get("mfc_url") or None
        imported += 1

    db.commit()
    return MessageResponse(
        message=f"Imported {imported} entr(ies), skipped {skipped}",
        detail={"imported": imported, "skipped": skipped},
    )
