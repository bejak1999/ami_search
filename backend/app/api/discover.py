"""Discovery: find AmiAmi listings by MyFigureCollection tags.

Two modes, because they answer different questions.

``local``  Filter the items this instance already knows about by tag. Instant,
           and every hit is something you can buy right now.
``mfc``    Ask MyFigureCollection which items carry the tags, then look each
           one up on the shop. Slower and rate-limited, but it reaches figures
           nobody here has searched for yet.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, user_cost_profile
from ..enrichment.mfc import MfcError, client as mfc_client
from ..models import CostProfile, DiscoverySeed, Item, ItemTag, Tag, TagKind, User, utcnow
from ..providers import ProviderError, SearchQuery, get_provider
from ..schemas import ItemOut, MessageResponse
from ..services import catalog, enrich, feed
from .serializers import item_out, register_images

log = logging.getLogger(__name__)
router = APIRouter(prefix="/discover", tags=["discovery"])


@router.get("/feed", response_model=MessageResponse)
def discovery_feed(
    provider: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> MessageResponse:
    """Things worth knowing about, without having to ask first.

    Rails drawn from the user's own watches and wishlist come first when there
    is anything to fill them; the rest work on a brand new account too.
    """
    rails = feed.build(db, user, provider)
    for rail in rails:
        register_images(db, rail.items)

    return MessageResponse(
        message=f"{len(rails)} rail(s)",
        detail={
            "rails": [
                {
                    "key": rail.key,
                    "title": rail.title,
                    "subtitle": rail.subtitle,
                    "icon": rail.icon,
                    "explore": rail.explore,
                    "items": [
                        item_out(db, item, user=user, profile=profile, with_context=True).model_dump(
                            mode="json"
                        )
                        for item in rail.items
                    ],
                }
                for rail in rails
            ]
        },
    )


@router.get("/tags", response_model=list[dict])
def list_tags(
    q: str | None = None,
    kind: TagKind | None = None,
    limit: int = Query(default=40, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> list[dict]:
    """Tag autocomplete, ranked by how many local items carry each tag."""
    stmt = select(Tag)
    if q:
        pattern = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(Tag.name.ilike(pattern), Tag.slug.ilike(pattern)))
    if kind is not None:
        stmt = stmt.where(Tag.kind == kind)
    rows = db.execute(
        stmt.order_by(Tag.usage_count.desc(), Tag.name).limit(limit)
    ).scalars()
    return [
        {
            "id": tag.id,
            "kind": tag.kind.value,
            "slug": tag.slug,
            "name": tag.name,
            "usage_count": tag.usage_count,
            "is_auto": tag.is_auto,
            "mfc_url": f"https://myfigurecollection.net/tag/{tag.mfc_id}" if tag.mfc_id else None,
        }
        for tag in rows
    ]


@router.get("/local", response_model=list[ItemOut])
def discover_local(
    tags: list[str] = Query(default=[], description="Tag slugs, combined with AND"),
    in_stock: bool | None = None,
    condition: str | None = None,
    max_price: float | None = None,
    sort: str = Query(default="newest", pattern="^(newest|price_asc|price_desc|discount)$"),
    limit: int = Query(default=48, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> list[ItemOut]:
    """Items in the local catalogue carrying every requested tag."""
    stmt = select(Item)

    slugs = [t.strip() for t in tags if t.strip()]
    if slugs:
        tag_ids = list(
            db.execute(select(Tag.id).where(Tag.slug.in_(slugs))).scalars().all()
        )
        if not tag_ids:
            return []
        # AND semantics: the item must carry as many of the requested tags as
        # were actually resolved.
        stmt = (
            stmt.join(ItemTag, ItemTag.item_id == Item.id)
            .where(ItemTag.tag_id.in_(tag_ids))
            .group_by(Item.id)
            .having(func.count(func.distinct(ItemTag.tag_id)) == len(tag_ids))
        )

    if in_stock is not None:
        stmt = stmt.where(Item.in_stock.is_(in_stock))
    if condition in ("new", "preowned"):
        stmt = stmt.where(Item.condition == condition)
    if max_price is not None:
        stmt = stmt.where(Item.current_price <= max_price)

    if sort == "price_asc":
        stmt = stmt.order_by(Item.current_price.asc().nulls_last())
    elif sort == "price_desc":
        stmt = stmt.order_by(Item.current_price.desc().nulls_last())
    elif sort == "discount":
        stmt = stmt.order_by((Item.current_price / Item.list_price).asc().nulls_last())
    else:
        stmt = stmt.order_by(Item.last_seen_at.desc())

    rows = list(db.execute(stmt.offset(offset).limit(limit)).scalars().all())
    register_images(db, rows)
    return [item_out(db, item, user=user, profile=profile, with_context=True) for item in rows]


@router.get("/item/{item_id}/tags", response_model=list[dict])
def item_tags(
    item_id: int, db: Session = Depends(get_db), _user: User = Depends(current_user)
) -> list[dict]:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    return [
        {
            "id": tag.id,
            "kind": tag.kind.value,
            "slug": tag.slug,
            "name": tag.name,
            "usage_count": tag.usage_count,
            "is_auto": tag.is_auto,
        }
        for tag in sorted(item.tags, key=lambda t: (t.kind.value, t.name))
    ]


@router.post("/item/{item_id}/enrich", response_model=MessageResponse)
def enrich_one(
    item_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> MessageResponse:
    """Look this item up on MyFigureCollection right now."""
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    linked = enrich.enrich_item(db, item, force=force)
    if not linked:
        return MessageResponse(
            ok=False,
            message="No confident MyFigureCollection match found for this item",
        )
    return MessageResponse(
        message=f"Linked to MyFigureCollection ({item.mfc_matched_by} match)",
        detail={
            "mfc_id": item.mfc_id,
            "mfc_url": item.mfc_url,
            "matched_by": item.mfc_matched_by,
            "confidence": item.mfc_confidence,
            "tags": len(item.tags),
        },
    )


def _seed_title_to_query(title: str) -> str:
    """Turn an MFC listing title into something AmiAmi search can use.

    MFC titles read "Origin - Character - Category - Variant (Company)". The
    origin plus the character is by far the most searchable part.
    """
    head = title.split("(")[0]
    parts = [p.strip() for p in head.split(" - ") if p.strip()]
    return " ".join(parts[:2]) if parts else head.strip()


@router.post("/mfc", response_model=MessageResponse)
def discover_via_mfc(
    payload: dict,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    profile: CostProfile = Depends(user_cost_profile),
) -> MessageResponse:
    """Browse MFC by tag, then find the matching listings on the shop.

    This is the slow path. MFC is scraped at a deliberately low rate, so the
    listing page is cached per tag combination and only a bounded number of
    shop lookups happen per call.
    """
    slugs = [str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()][:6]
    if not slugs:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Pick at least one tag"
        )

    page = max(1, int(payload.get("page") or 1))
    figures_only = bool(payload.get("figures_only", True))
    lookups = min(int(payload.get("lookups") or 12), 25)
    provider_id = str(payload.get("provider") or "amiami")
    tag_key = "|".join(sorted(slugs)) + f"#{page}#{int(figures_only)}"

    try:
        listings, total_pages = mfc_client.browse_tags(
            slugs, page=page, root=1 if figures_only else None
        )
    except MfcError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    provider = get_provider(provider_id)
    results: list[dict] = []
    looked_up = 0

    for rank, listing in enumerate(listings):
        seed = db.execute(
            select(DiscoverySeed).where(
                DiscoverySeed.tag_key == tag_key, DiscoverySeed.mfc_id == listing.id
            )
        ).scalar_one_or_none()
        if seed is None:
            seed = DiscoverySeed(
                tag_key=tag_key,
                mfc_id=listing.id,
                title=listing.title,
                image_url=listing.image_url,
                category=str(listing.category),
                rank=rank,
            )
            db.add(seed)
            db.flush()

        matched: Item | None = (
            db.get(Item, seed.matched_item_id) if seed.matched_item_id else None
        )

        if matched is None and seed.lookup_state == "pending" and looked_up < lookups:
            looked_up += 1
            matched = _lookup_on_shop(db, provider, seed, listing.title)

        results.append(
            {
                "mfc_id": listing.id,
                "mfc_url": f"https://myfigurecollection.net/item/{listing.id}",
                "mfc_title": listing.title,
                "mfc_image": listing.image_url,
                "state": seed.lookup_state,
                "item": (
                    item_out(db, matched, user=user, profile=profile, with_context=True).model_dump(
                        mode="json"
                    )
                    if matched
                    else None
                ),
            }
        )

    db.commit()
    available = sum(1 for r in results if r["item"])
    return MessageResponse(
        message=f"{len(results)} MyFigureCollection item(s), {available} found on {provider.name}",
        detail={
            "page": page,
            "total_pages": total_pages,
            "tags": slugs,
            "shop_lookups_used": looked_up,
            "results": results,
        },
    )


def _lookup_on_shop(db: Session, provider, seed: DiscoverySeed, title: str) -> Item | None:
    """Search the shop for one MFC listing and record the outcome."""
    query = _seed_title_to_query(title)
    if not query:
        seed.lookup_state = "unmatched"
        return None

    try:
        result = provider.search(SearchQuery(keywords=query, per_page=10))
    except ProviderError as exc:
        log.debug("Shop lookup failed for %r: %s", query, exc)
        seed.lookup_state = "pending"
        return None

    best, best_score = None, 0.0
    for normalized in result.items:
        score = enrich.title_confidence(title, normalized.name)
        if score > best_score:
            best, best_score = normalized, score

    seed.fetched_at = utcnow()
    if best is None or best_score < 0.35:
        seed.lookup_state = "unmatched"
        return None

    item, _ = catalog.upsert_item(db, best, commit=False)
    db.flush()
    seed.matched_item_id = item.id
    seed.lookup_state = "matched"
    return item


@router.get("/stats", response_model=MessageResponse)
def discovery_stats(
    db: Session = Depends(get_db), _user: User = Depends(current_user)
) -> MessageResponse:
    """How much of the catalogue has been cross-referenced so far."""
    stats = enrich.tag_stats(db)
    top = db.execute(
        select(Tag).where(Tag.kind == TagKind.tag).order_by(Tag.usage_count.desc()).limit(20)
    ).scalars()
    stats["top_tags"] = [
        {"slug": t.slug, "name": t.name, "usage_count": t.usage_count} for t in top
    ]
    return MessageResponse(message="ok", detail=stats)


@router.post("/enrich/run", response_model=MessageResponse)
def run_enrichment(
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    _user: User = Depends(current_user),
) -> MessageResponse:
    """Kick off one enrichment batch by hand."""
    outcome = enrich.run_batch(db, limit=limit)
    return MessageResponse(
        message=f"{outcome['linked']} linked, {outcome['unmatched']} without a confident match",
        detail=outcome,
    )
