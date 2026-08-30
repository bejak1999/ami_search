"""Live shop search and item lookup."""
from __future__ import annotations

import logging
import math

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, user_cost_profile
from ..models import CostProfile, User
from ..providers import (
    ItemNotFound,
    ProviderError,
    SearchQuery,
    all_providers,
    detect_provider_from_url,
    get_provider,
)
from ..schemas import (
    ItemOut,
    LocalSearchRequest,
    MessageResponse,
    ProviderInfo,
    ResolveRequest,
    SearchRequest,
    SearchResponse,
)
from ..services import catalog, localsearch
from .serializers import item_from_normalized

log = logging.getLogger(__name__)
router = APIRouter(prefix="/search", tags=["search"])


def provider_info(provider) -> ProviderInfo:
    health = provider.health()
    return ProviderInfo(
        id=provider.id,
        name=provider.name,
        home_url=provider.home_url,
        currency=provider.currency,
        description=provider.description,
        supports_facets=provider.supports_facets,
        healthy=not health["circuit"]["open"],
        circuit=health["circuit"],
        rate_per_minute=health["rate_per_minute"],
        last_latency_ms=health["last_latency_ms"],
    )


@router.get("/providers", response_model=list[ProviderInfo])
def providers() -> list[ProviderInfo]:
    return [provider_info(p) for p in all_providers()]


@router.post("", response_model=SearchResponse)
def search(
    payload: SearchRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> SearchResponse:
    """Search a shop live and persist whatever comes back.

    Persisting search hits is deliberate: it seeds price history for items the
    user has not explicitly tracked yet, so the first chart is not empty.
    """
    try:
        provider = get_provider(payload.provider)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    query = SearchQuery(
        keywords=payload.q.strip(),
        page=payload.page,
        per_page=payload.per_page,
        condition=payload.condition,
        stock_filter=payload.stock,
        sort=payload.sort,
        category_id=payload.category_id,
        maker_id=payload.maker_id,
        series_id=payload.series_id,
        min_price=payload.min_price,
        max_price=payload.max_price,
        exclude_keywords=payload.exclude,
        extra={"on_sale": payload.on_sale} if payload.on_sale else {},
    )

    try:
        result = provider.search(query)
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    items: list[ItemOut] = []
    for normalized in result.items:
        stored, _ = catalog.upsert_item(db, normalized, commit=False)
        items.append(
            item_from_normalized(db, normalized, user=user, profile=profile, stored=stored)
        )
    db.commit()

    # Re-serialise now that ids exist, and attach per-user context flags.
    from .serializers import item_out

    hydrated = []
    for payload_item, normalized in zip(items, result.items, strict=False):
        stored = catalog.get_item(db, normalized.provider, normalized.code)
        hydrated.append(
            item_out(db, stored, user=user, profile=profile, with_context=True)
            if stored
            else payload_item
        )

    return SearchResponse(
        items=hydrated,
        total=result.total,
        page=result.page,
        per_page=result.per_page,
        pages=max(1, math.ceil(result.total / max(1, result.per_page))),
        facets=result.facets,
        took_ms=result.took_ms,
        provider=provider.id,
    )


@router.post("/local", response_model=SearchResponse)
def search_local(
    payload: LocalSearchRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> SearchResponse:
    """Search this instance's catalogue rather than the shop.

    Larger than the shop's own search, because listings AmiAmi deletes when
    they sell are kept here, and able to sort by things the shop cannot know
    about its own removed listings, such as the lowest price ever recorded.
    """
    import time

    started = time.monotonic()
    # The blocklist comes from the signed-in profile, never from the request,
    # so a crafted body cannot widen someone else's filter or narrow theirs.
    payload.blocked_terms = list(profile.blocked_terms or [])
    payload.blocked_tags = list(profile.blocked_tags or [])
    result = localsearch.search(db, payload)
    from .serializers import item_out, register_images

    register_images(db, result.items)

    return SearchResponse(
        items=[
            item_out(db, item, user=user, profile=profile, with_context=True)
            for item in result.items
        ],
        total=result.total,
        page=result.page,
        per_page=result.per_page,
        pages=result.pages,
        facets={},
        took_ms=int((time.monotonic() - started) * 1000),
        provider=payload.provider or "local",
    )


@router.get("/local/tags", response_model=list[dict])
def local_tags(
    q: str | None = None,
    kind: str | None = None,
    limit: int = Query(default=60, ge=1, le=300),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> list[dict]:
    """Tags available for filtering, most used first."""
    return localsearch.facet_tags(db, limit=limit, kinds=[kind] if kind else None, q=q)


@router.get("/local/summary", response_model=MessageResponse)
def local_summary(
    provider: str | None = None,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> MessageResponse:
    """Size of the local catalogue, including how much of it the shop dropped."""
    return MessageResponse(message="ok", detail=localsearch.catalogue_summary(db, provider))


@router.post("/resolve", response_model=ItemOut)
def resolve(
    payload: ResolveRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> ItemOut:
    """Take a pasted shop URL or bare product code and return the item.

    This is what powers the 'paste an AmiAmi link' box, mirroring how people
    actually find something they want to track.
    """
    detected = detect_provider_from_url(payload.input.strip())
    if detected is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not recognise that as a shop link or product code",
        )

    provider_id, code = detected
    provider = get_provider(provider_id)
    try:
        normalized = provider.get_item(code)
    except ItemNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    stored, _ = catalog.upsert_item(db, normalized)
    from .serializers import item_out

    return item_out(db, stored, user=user, profile=profile, with_context=True)


@router.get("/suggest", response_model=MessageResponse)
def suggest(
    q: str,
    provider: str = "amiami",
    _user: User = Depends(current_user),
) -> MessageResponse:
    """Facet suggestions for the search sidebar."""
    try:
        options = get_provider(provider).suggest(q)
    except (KeyError, ProviderError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return MessageResponse(
        message="ok",
        detail=[{"id": o.id, "name": o.name, "count": o.count} for o in options],
    )
