"""Stored item detail, price history and manual refresh."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, user_cost_profile
from ..models import CostProfile, Item, User
from ..providers import ItemNotFound, ProviderError, get_provider
from ..schemas import (
    CostBreakdownOut,
    ItemHistoryOut,
    ItemOut,
    PricePointOut,
    ShelfLifeOut,
)
from ..services import catalog, landed_cost, shelflife
from .serializers import item_out, register_images

router = APIRouter(prefix="/items", tags=["items"])


def _get_or_404(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return item


@router.get("", response_model=list[ItemOut])
def list_items(
    q: str | None = None,
    provider: str | None = None,
    in_stock: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> list[ItemOut]:
    """Browse the local catalogue, i.e. everything this instance has ever seen."""
    stmt = select(Item)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Item.name.ilike(pattern),
                Item.code.ilike(pattern),
                Item.maker.ilike(pattern),
                Item.series.ilike(pattern),
                Item.character.ilike(pattern),
            )
        )
    if provider:
        stmt = stmt.where(Item.provider == provider)
    if in_stock is not None:
        stmt = stmt.where(Item.in_stock.is_(in_stock))

    rows = list(
        db.execute(stmt.order_by(Item.last_seen_at.desc()).offset(offset).limit(limit)).scalars()
    )
    register_images(db, rows)
    return [item_out(db, item, user=user, profile=profile, with_context=True) for item in rows]


@router.get("/{item_id}", response_model=ItemOut)
def get_item(
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> ItemOut:
    return item_out(
        db,
        _get_or_404(db, item_id),
        user=user,
        profile=profile,
        with_context=True,
        with_counterpart=True,
    )


@router.get("/{item_id}/history", response_model=ItemHistoryOut)
def get_history(
    item_id: int,
    days: int = Query(default=365, ge=1, le=3650),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> ItemHistoryOut:
    item = _get_or_404(db, item_id)
    points = catalog.history(db, item_id, days=days)
    return ItemHistoryOut(
        item=item_out(
            db, item, user=user, profile=profile, with_context=True, with_counterpart=True
        ),
        points=[PricePointOut.model_validate(p) for p in points],
        stats=catalog.price_stats(db, item_id),
    )


@router.get("/{item_id}/shelf-life", response_model=ShelfLifeOut)
def get_shelf_life(
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> ShelfLifeOut:
    """Every copy we have seen under this product code, and what it tells us.

    AmiAmi deletes a pre-owned copy when it sells, so a copy that is missing
    from a later fetch is a sale. What we cannot see is exactly when: the
    answer is bracketed by the two looks either side of it, and every figure
    here carries those bounds rather than pretending to a precision the poll
    rate does not support.
    """
    item = _get_or_404(db, item_id)
    return ShelfLifeOut(**shelflife.summary(db, item))


@router.get("/{item_id}/landed", response_model=CostBreakdownOut)
def get_landed(
    item_id: int,
    quantity: int = Query(default=1, ge=1, le=50),
    currency: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> CostBreakdownOut:
    """Full landed-cost breakdown, the tooltip behind the estimated total."""
    item = _get_or_404(db, item_id)
    breakdown = landed_cost.estimate(
        db,
        item.current_price,
        item.currency,
        profile,
        target_currency=(currency or user.display_currency or "EUR").upper(),
        item=item,
        quantity=quantity,
    )
    if breakdown is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No price or no exchange rate available for this item yet",
        )
    return CostBreakdownOut(**breakdown.as_dict())


@router.post("/{item_id}/counterpart", response_model=ItemOut)
def fetch_counterpart(
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> ItemOut:
    """Pull in the same figure's other-condition listing from the shop.

    A figure is frequently sold pre-owned while its new listing is still open,
    so comparing the two price histories is the question people actually have.
    This fetches the counterpart on demand rather than guessing that it exists.
    """
    item = _get_or_404(db, item_id)
    from .serializers import counterpart_code

    code = counterpart_code(item.code)
    existing = catalog.get_item(db, item.provider, code)
    if existing is not None:
        return item_out(
            db, existing, user=user, profile=profile, with_context=True, with_counterpart=True
        )

    provider = get_provider(item.provider)
    try:
        normalized = provider.get_item(code)
    except ItemNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{provider.name} has no {code} listing for this figure",
        ) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    stored, _ = catalog.upsert_item(db, normalized)
    return item_out(
        db, stored, user=user, profile=profile, with_context=True, with_counterpart=True
    )


@router.post("/{item_id}/refresh", response_model=ItemOut)
def refresh_item(
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> ItemOut:
    """Re-poll one item right now."""
    item = _get_or_404(db, item_id)
    provider = get_provider(item.provider)
    try:
        normalized = provider.get_item(item.code)
    except ItemNotFound:
        catalog.mark_unavailable(db, item)
        return item_out(db, item, user=user, profile=profile, with_context=True)
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    updated, _ = catalog.upsert_item(db, normalized)
    return item_out(db, updated, user=user, profile=profile, with_context=True)
