"""Linking catalogue items to MyFigureCollection and building the tag index.

Matching strategy, best first:
  1. JAN barcode. AmiAmi publishes one for almost every figure and MFC
     redirects a barcode search straight to the item, so this is exact.
  2. Title search, scored by token overlap. Flagged as a weaker match so the
     UI can show it as "probable" rather than fact.

Every enriched item contributes its tags to a local index, which is what the
discovery page actually queries. Scraping MFC once per item and then joining
locally is both faster for the user and far kinder to MFC.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..enrichment.mfc import MfcError, MfcItem, MfcNotFound, client
from ..models import CollectionEntry, Item, ItemTag, Tag, TagKind, Watch, WatchSeenItem, utcnow

log = logging.getLogger(__name__)

#: Give up on an item after this many failed attempts.
MAX_ATTEMPTS = 3
#: Re-check an already linked item at most this often.
REFRESH_AFTER = timedelta(days=30)
#: Below this token overlap a title match is rejected outright.
MIN_TITLE_CONFIDENCE = 0.45

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_NOISE_TOKENS = {
    "complete", "figure", "figures", "scale", "bonus", "ver", "version",
    "limited", "exclusive", "pre", "owned", "preowned", "new", "the", "and",
    "with", "set", "item", "box", "japan", "japanese", "anime", "amiami",
}


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall((text or "").lower())
        if len(token) > 1 and token not in _NOISE_TOKENS
    }


def title_confidence(shop_name: str, mfc_title: str) -> float:
    """Jaccard-ish overlap, weighted toward covering the shop title."""
    left, right = _tokens(shop_name), _tokens(mfc_title)
    if not left or not right:
        return 0.0
    overlap = len(left & right)
    # Cover matters more than symmetry: the MFC title carries extra words
    # (company, scale) that the shop title legitimately lacks.
    return (overlap / len(left)) * 0.7 + (overlap / len(left | right)) * 0.3


def get_or_create_tag(
    db: Session, kind: TagKind, slug: str, name: str, mfc_id: int | None = None, is_auto: bool = False
) -> Tag:
    slug = slug.strip()[:160]
    tag = db.execute(
        select(Tag).where(Tag.kind == kind, Tag.slug == slug)
    ).scalar_one_or_none()
    if tag is None:
        tag = Tag(kind=kind, slug=slug, name=name[:200] or slug, mfc_id=mfc_id, is_auto=is_auto)
        db.add(tag)
        db.flush()
    else:
        if mfc_id and not tag.mfc_id:
            tag.mfc_id = mfc_id
        if name and tag.name != name[:200]:
            tag.name = name[:200]
    return tag


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:160]


def _attach(db: Session, item: Item, tag: Tag) -> bool:
    exists = db.execute(
        select(ItemTag.id).where(ItemTag.item_id == item.id, ItemTag.tag_id == tag.id)
    ).first()
    if exists:
        return False
    db.add(ItemTag(item_id=item.id, tag_id=tag.id))
    tag.usage_count += 1
    return True


_ENTRY_KINDS = (
    ("origins", TagKind.origin),
    ("characters", TagKind.character),
    ("companies", TagKind.company),
    ("artists", TagKind.artist),
    ("materials", TagKind.material),
    ("classifications", TagKind.classification),
)


def apply_mfc_item(db: Session, item: Item, mfc: MfcItem, matched_by: str, confidence: float) -> int:
    """Write the MFC link and its tags onto a catalogue item. Returns new tags."""
    item.mfc_id = mfc.id
    item.mfc_url = mfc.url
    item.mfc_matched_by = matched_by
    item.mfc_confidence = round(confidence, 3)
    item.mfc_fetched_at = utcnow()

    added = 0
    for attribute, kind in _ENTRY_KINDS:
        for entry in getattr(mfc, attribute):
            tag = get_or_create_tag(
                db, kind, _slugify(entry.name), entry.name, mfc_id=entry.id or None
            )
            added += int(_attach(db, item, tag))

    for mfc_tag in mfc.tags:
        tag = get_or_create_tag(
            db,
            TagKind.tag,
            mfc_tag.slug,
            mfc_tag.name,
            mfc_id=mfc_tag.mfc_id,
            is_auto=mfc_tag.is_auto,
        )
        added += int(_attach(db, item, tag))

    # Backfill the shop record from MFC where AmiAmi left gaps.
    if not item.series and mfc.origins:
        item.series = mfc.origins[0].name[:255]
    if not item.character and mfc.characters:
        item.character = mfc.characters[0].name[:255]
    if not item.maker and mfc.companies:
        item.maker = mfc.companies[0].name[:255]

    db.commit()
    # ItemTag rows were inserted directly, so the loaded relationship is stale.
    db.expire(item, ["tags"])
    return added


def enrich_item(db: Session, item: Item, force: bool = False) -> bool:
    """Link one item to MFC. Returns True when a link was established."""
    if item.mfc_id and not force:
        age = datetime.now(timezone.utc) - (
            item.mfc_fetched_at.replace(tzinfo=timezone.utc)
            if item.mfc_fetched_at and item.mfc_fetched_at.tzinfo is None
            else item.mfc_fetched_at or datetime.min.replace(tzinfo=timezone.utc)
        )
        if age < REFRESH_AFTER:
            return True

    if item.mfc_attempts >= MAX_ATTEMPTS and not force:
        return False

    item.mfc_attempts += 1
    db.commit()

    try:
        if item.jan_code:
            found = client.find_by_jan(item.jan_code)
            if found is not None:
                apply_mfc_item(db, item, found, matched_by="jan", confidence=1.0)
                log.info("Linked %s to MFC %s by barcode", item.code, found.id)
                return True

        # No barcode, or the barcode search was ambiguous. Fall back to titles.
        listings = client.search(item.name, root=None)
        best, best_score = None, 0.0
        for listing in listings[:8]:
            score = title_confidence(item.name, listing.title)
            if score > best_score:
                best, best_score = listing, score

        if best is None or best_score < MIN_TITLE_CONFIDENCE:
            item.mfc_fetched_at = utcnow()
            db.commit()
            log.debug("No confident MFC match for %s (best %.2f)", item.code, best_score)
            return False

        detail = client.get_item(best.id)
        apply_mfc_item(db, item, detail, matched_by="title", confidence=best_score)
        log.info("Linked %s to MFC %s by title (%.2f)", item.code, detail.id, best_score)
        return True

    except MfcNotFound:
        item.mfc_fetched_at = utcnow()
        db.commit()
        return False
    except MfcError as exc:
        log.warning("MFC enrichment failed for %s: %s", item.code, exc)
        db.commit()
        return False


def pending_items(db: Session, limit: int = 20) -> list[Item]:
    """Items still missing an MFC link, most interesting first.

    Anything on a wishlist or matched by a watch jumps the queue, because
    those are the items a person will actually open.
    """
    interesting = set(
        db.execute(select(CollectionEntry.item_id)).scalars().all()
    ) | set(
        db.execute(
            select(WatchSeenItem.item_id).join(Watch, Watch.id == WatchSeenItem.watch_id)
        )
        .scalars()
        .all()
    )

    stmt = (
        select(Item)
        .where(
            Item.mfc_id.is_(None),
            Item.mfc_attempts < MAX_ATTEMPTS,
        )
        .order_by(Item.last_seen_at.desc())
        .limit(limit * 4)
    )
    candidates = list(db.execute(stmt).scalars().all())
    candidates.sort(key=lambda i: (i.id not in interesting, -(i.id or 0)))
    return candidates[:limit]


def run_batch(db: Session, limit: int = 10) -> dict:
    """One pass of the background enrichment job."""
    linked, missed = 0, 0
    for item in pending_items(db, limit=limit):
        if enrich_item(db, item):
            linked += 1
        else:
            missed += 1
    if linked or missed:
        log.info("MFC enrichment batch: %s linked, %s unmatched", linked, missed)
    return {"linked": linked, "unmatched": missed}


def tag_stats(db: Session) -> dict:
    total_tags = int(db.execute(select(func.count(Tag.id))).scalar_one() or 0)
    linked_items = int(
        db.execute(select(func.count(Item.id)).where(Item.mfc_id.is_not(None))).scalar_one() or 0
    )
    pending = int(
        db.execute(
            select(func.count(Item.id)).where(
                Item.mfc_id.is_(None), Item.mfc_attempts < MAX_ATTEMPTS
            )
        ).scalar_one()
        or 0
    )
    return {
        "tags": total_tags,
        "linked_items": linked_items,
        "pending_items": pending,
        "client": client.status(),
    }
