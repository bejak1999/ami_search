"""Local cache for product photos.

AmiAmi deletes a pre-owned listing the moment it sells, and the images go with
it. Every other fact about the figure survives in this database, so without a
local copy the record of something that sold is a row with a broken picture,
at exactly the point the history becomes worth having. When the same figure is
listed pre-owned again later, the item is already here and only its price
needs updating; the photo should still be there too.

Files are content-addressed by a hash of their source URL, so the same photo is
never stored twice and the path can be derived without touching the database.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import CachedImage, CollectionEntry, Item, Watch, WatchSeenItem, utcnow
from ..providers.ratelimit import TokenBucket

log = logging.getLogger(__name__)

#: AmiAmi's own path segments tell us which size we are looking at.
THUMB_MARKERS = ("/thumb300/", "/thumb/", "/rthumb/")
MAX_BYTES = 12 * 1024 * 1024

_bucket = TokenBucket(rate_per_minute=int(settings.image_cache_requests_per_minute), burst=15)
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def cache_root() -> Path:
    return Path(settings.data_dir) / "images"


def key_for(url: str) -> str:
    """Stable short key for a source URL."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def kind_for(url: str) -> str:
    return "thumb" if any(marker in url for marker in THUMB_MARKERS) else "main"


def path_for(key: str, content_type: str = "image/jpeg") -> Path:
    """Sharded so no single directory ends up with a hundred thousand files."""
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(content_type, ".jpg")
    return cache_root() / key[:2] / key[2:4] / f"{key}{extension}"


def public_url(url: str | None) -> str | None:
    """The route the UI should use for a source image."""
    if not url or not settings.image_cache_enabled:
        return url
    if not url.startswith("http"):
        return url
    return f"/api/images/{key_for(url)}"


@dataclass(slots=True)
class StoredImage:
    path: Path
    content_type: str
    bytes: int
    from_cache: bool


def _lock_for(key: str) -> threading.Lock:
    """One lock per image, so a grid loading forty photos fetches each once."""
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _record(db: Session, key: str, url: str) -> CachedImage:
    row = db.execute(select(CachedImage).where(CachedImage.key == key)).scalar_one_or_none()
    if row is None:
        row = CachedImage(key=key, source_url=url, kind=kind_for(url))
        db.add(row)
        db.flush()
    return row


def _download(url: str) -> tuple[bytes, str]:
    """Fetch one image. Uses the same Chrome impersonation as the API calls.

    The browser would fetch these directly anyway, so routing them through the
    server does not add load upstream; caching means strictly fewer requests
    over time.
    """
    from curl_cffi import requests as curl_requests

    _bucket.acquire(timeout=30.0)
    response = curl_requests.get(
        url,
        impersonate="chrome",
        timeout=30,
        headers={"Referer": "https://www.amiami.com/", "Accept": "image/avif,image/webp,image/*"},
    )
    if response.status_code == 404:
        raise FileNotFoundError("origin no longer serves this image")
    if response.status_code >= 400:
        raise RuntimeError(f"origin returned {response.status_code}")

    content = response.content
    if not content:
        raise RuntimeError("origin returned an empty body")
    if len(content) > MAX_BYTES:
        raise RuntimeError(f"image is larger than the {MAX_BYTES // 1024 // 1024} MB limit")

    content_type = (response.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        raise RuntimeError(f"origin returned {content_type}, not an image")
    return content, content_type


def fetch(db: Session, url: str, *, touch: bool = True) -> StoredImage | None:
    """Return the local copy, downloading it first if need be.

    None means the image is unavailable and will stay that way: the origin has
    deleted it and we never had a copy.
    """
    if not settings.image_cache_enabled or not url.startswith("http"):
        return None

    key = key_for(url)
    with _lock_for(key):
        row = _record(db, key, url)

        if row.fetched_at and not row.gone:
            path = path_for(key, row.content_type)
            if path.is_file():
                if touch:
                    row.last_used_at = utcnow()
                    row.use_count += 1
                    db.commit()
                return StoredImage(path, row.content_type, row.bytes, from_cache=True)
            # The row says cached but the file went missing; fetch it again.
            row.fetched_at = None

        if row.gone:
            return None
        if row.attempts >= 3:
            return None

        row.attempts += 1
        db.commit()

        try:
            content, content_type = _download(url)
        except FileNotFoundError as exc:
            # Gone upstream. If we never cached it, the picture is simply lost.
            row.gone = True
            row.last_error = str(exc)
            db.commit()
            log.info("Image no longer available upstream: %s", url)
            return None
        except Exception as exc:  # noqa: BLE001 - a failed image must never 500 a page
            row.last_error = str(exc)[:300]
            db.commit()
            log.debug("Could not cache %s: %s", url, exc)
            return None

        path = path_for(key, content_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and move, so a crash cannot leave a partial
        # file that later looks like a valid cache entry.
        staging = path.with_suffix(path.suffix + ".part")
        staging.write_bytes(content)
        staging.replace(path)

        row.content_type = content_type
        row.bytes = len(content)
        row.fetched_at = utcnow()
        row.last_used_at = utcnow()
        row.attempts = 0
        row.last_error = None
        row.gone = False
        db.commit()

        return StoredImage(path, content_type, len(content), from_cache=False)


def thumbnail_of(url: str) -> str | None:
    """The small version of a product photo, derived from its URL.

    AmiAmi serves the same picture from parallel paths, and which one an item
    carries depends purely on where we saw it: search results give
    ``/thumb300/``, the detail endpoint gives ``/main/``. Converting between
    them means a grid can load 4 KB thumbnails even for items we only ever
    fetched in detail, instead of 80 KB full images.
    """
    if "/thumb300/" in url:
        return url
    if "/main/" in url:
        return url.replace("/main/", "/thumb300/")
    return None


def full_of(url: str) -> str | None:
    """The large version of a product photo, derived from its URL."""
    if "/main/" in url:
        return url
    if "/thumb300/" in url:
        return url.replace("/thumb300/", "/main/")
    return None


def urls_for_item(item: Item, include_full: bool | None = None) -> list[str]:
    """Which of an item's photos are worth keeping, cheapest first.

    The thumbnail always, because it is tiny and it is what grids, alerts and
    the wishlist show. The full image too when configured, since that is what
    the item page needs and it is the one that cannot be recovered once the
    listing is gone.
    """
    include_full = settings.image_cache_full_images if include_full is None else include_full
    wanted: list[str] = []

    def add(url: str | None) -> None:
        if url and url not in wanted:
            wanted.append(url)

    primary = item.image_url or next(iter(item.images or []), None)
    if not primary:
        return []

    add(thumbnail_of(primary) or primary)
    if include_full:
        add(full_of(primary) or primary)
    return wanted


def _priority_item_ids(db: Session, limit: int) -> list[int]:
    """Items whose pictures matter most, should the cache fall behind.

    Anything on a wishlist or matched by a watch comes first: those are the
    ones a person will open, and the ones whose listing disappearing actually
    costs them something.
    """
    wanted: list[int] = []
    seen: set[int] = set()

    for stmt in (
        select(CollectionEntry.item_id),
        select(WatchSeenItem.item_id)
        .join(Watch, Watch.id == WatchSeenItem.watch_id)
        .where(Watch.enabled.is_(True)),
        # Then whatever is currently buyable, since those are the listings that
        # can vanish without warning.
        select(Item.id).where(Item.in_stock.is_(True)).order_by(Item.last_seen_at.desc()),
        select(Item.id).order_by(Item.last_seen_at.desc()),
    ):
        for item_id in db.execute(stmt.limit(limit * 4)).scalars():
            if item_id not in seen:
                seen.add(item_id)
                wanted.append(item_id)
            if len(wanted) >= limit * 4:
                break
        if len(wanted) >= limit * 4:
            break
    return wanted


def prefetch(db: Session, limit: int | None = None) -> dict:
    """Cache the photos of items that do not have theirs yet."""
    if not settings.image_cache_enabled:
        return {"fetched": 0, "skipped": 0, "reason": "disabled"}

    limit = limit or settings.image_prefetch_batch
    cached_keys = {
        row for row in db.execute(select(CachedImage.key)).scalars().all()
    }

    fetched = failed = 0
    for item_id in _priority_item_ids(db, limit):
        item = db.get(Item, item_id)
        if item is None:
            continue
        for url in urls_for_item(item):
            if key_for(url) in cached_keys:
                continue
            if fetch(db, url, touch=False):
                fetched += 1
            else:
                failed += 1
            if fetched >= limit:
                return {"fetched": fetched, "failed": failed}
    return {"fetched": fetched, "failed": failed}


def prune(db: Session, budget_bytes: int | None = None) -> dict:
    """Drop the least recently shown images once past the budget.

    Photos of items nobody has on a wishlist or a watch go first, because a
    catalogue thumbnail can always be fetched again while a sold-out listing's
    picture cannot.
    """
    budget = budget_bytes or int(settings.image_cache_max_gb * 1024**3)
    total = int(db.execute(select(func.coalesce(func.sum(CachedImage.bytes), 0))).scalar_one())
    if total <= budget:
        return {"pruned": 0, "freed_bytes": 0, "total_bytes": total, "budget_bytes": budget}

    protected = set(
        db.execute(select(CollectionEntry.item_id)).scalars().all()
    ) | set(
        db.execute(
            select(WatchSeenItem.item_id).join(Watch, Watch.id == WatchSeenItem.watch_id)
        )
        .scalars()
        .all()
    )
    protected_urls: set[str] = set()
    if protected:
        for item in db.execute(select(Item).where(Item.id.in_(protected))).scalars():
            protected_urls.update(urls_for_item(item))
    protected_keys = {key_for(u) for u in protected_urls}

    freed = pruned = 0
    candidates = db.execute(
        select(CachedImage).order_by(CachedImage.last_used_at.asc())
    ).scalars()

    for row in candidates:
        if total - freed <= budget:
            break
        if row.key in protected_keys:
            continue
        path = path_for(row.key, row.content_type)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        freed += row.bytes
        pruned += 1
        db.delete(row)

    db.commit()
    if pruned:
        log.info("Pruned %s cached image(s), freeing %.0f MB", pruned, freed / 1024 / 1024)
    return {
        "pruned": pruned,
        "freed_bytes": freed,
        "total_bytes": total - freed,
        "budget_bytes": budget,
    }


def stats(db: Session) -> dict:
    """What the cache holds, for the admin view."""
    total_rows, total_bytes = db.execute(
        select(func.count(CachedImage.id), func.coalesce(func.sum(CachedImage.bytes), 0))
    ).one()

    by_kind = {
        kind: {"count": int(count), "bytes": int(size)}
        for kind, count, size in db.execute(
            select(
                CachedImage.kind,
                func.count(CachedImage.id),
                func.coalesce(func.sum(CachedImage.bytes), 0),
            ).group_by(CachedImage.kind)
        ).all()
    }

    gone = int(
        db.execute(select(func.count(CachedImage.id)).where(CachedImage.gone.is_(True))).scalar_one()
    )
    items = int(db.execute(select(func.count(Item.id))).scalar_one())
    budget = int(settings.image_cache_max_gb * 1024**3)

    # How many photos the current catalogue would need in total.
    per_item = 2 if settings.image_cache_full_images else 1
    expected = items * per_item
    average = (int(total_bytes) / int(total_rows)) if total_rows else 0

    return {
        "enabled": settings.image_cache_enabled,
        "full_images": settings.image_cache_full_images,
        "count": int(total_rows),
        "bytes": int(total_bytes),
        "budget_bytes": budget,
        "percent_of_budget": round(int(total_bytes) / budget * 100, 1) if budget else 0.0,
        "by_kind": by_kind,
        "gone_upstream": gone,
        "items_known": items,
        "expected_images": expected,
        "coverage_percent": round(int(total_rows) / expected * 100, 1) if expected else 0.0,
        "average_bytes": int(average),
        "projected_bytes": int(average * expected) if average else 0,
        "path": str(cache_root()),
    }
