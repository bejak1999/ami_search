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
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    CachedImage,
    CollectionEntry,
    Condition,
    Item,
    Watch,
    WatchSeenItem,
    utcnow,
)
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

    from . import reqlog

    _bucket.acquire(timeout=30.0)
    response = curl_requests.get(
        url,
        impersonate="chrome",
        timeout=30,
        headers={"Referer": "https://www.amiami.com/", "Accept": "image/avif,image/webp,image/*"},
    )
    # Photos come from the shop's image host on their own allowance, but they
    # are the same connection to the same company and belong in the tally.
    reqlog.record("amiami", ok=response.status_code < 400, name="images")
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


def register(db: Session, urls: list[str], *, commit: bool = False) -> int:
    """Note that these photos exist, without downloading them.

    The public route is a hash of the source URL, which cannot be reversed, so
    the server has to have been told the mapping before it can serve or fetch
    anything. Recording it when the item is stored means one write per photo
    ever, rather than a lookup on every page render.
    """
    if not settings.image_cache_enabled:
        return 0

    wanted = {key_for(u): u for u in urls if u and u.startswith("http")}
    if not wanted:
        return 0

    known = set(
        db.execute(select(CachedImage.key).where(CachedImage.key.in_(wanted))).scalars().all()
    )
    added = 0
    for key, url in wanted.items():
        if key in known:
            continue
        db.add(CachedImage(key=key, source_url=url, kind=kind_for(url)))
        added += 1

    if added and commit:
        db.commit()
    return added


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


def _priority_items(db: Session) -> Iterator[int]:
    """Item ids in the order their pictures matter, most first.

    The order is by what is lost if we are too slow, not by what is nice to
    have. A pre-owned listing is one copy: when it sells, AmiAmi takes down
    the photographs of that copy, and they were the only pictures of the
    actual item rather than of the product. Nothing recreates them. A new
    item's stock photo, by contrast, stays up as long as the product exists
    and can be fetched whenever.

    A generator rather than a list, because the caller cannot know in advance
    how far down it has to read: most of what comes back is usually already on
    disk. Building a fixed-size list instead was the second half of the same
    bug - a wishlist big enough to fill the list meant the same already-cached
    items were offered every run, all of them skipped, and nothing behind them
    was ever reached.
    """
    seen: set[int] = set()
    for stmt in (
        select(CollectionEntry.item_id),
        select(WatchSeenItem.item_id)
        .join(Watch, Watch.id == WatchSeenItem.watch_id)
        .where(Watch.enabled.is_(True)),
        # Used copies on sale now: the irreplaceable ones. Newest first,
        # because a listing that has been up a month has shown it is in no
        # hurry, while one added today may be gone tonight.
        select(Item.id)
        .where(Item.in_stock.is_(True), Item.condition == Condition.preowned)
        .order_by(Item.first_seen_at.desc()),
        # Then anything else buyable, which can also vanish, though its photos
        # usually come back with the next restock.
        select(Item.id).where(Item.in_stock.is_(True)).order_by(Item.last_seen_at.desc()),
        select(Item.id).order_by(Item.last_seen_at.desc()),
    ):
        for item_id in db.execute(stmt).scalars():
            if item_id in seen:
                continue
            seen.add(item_id)
            yield item_id


#: How many photos to consider per run before giving up and leaving the rest
#: for the next one. Only a bound on the walk, not on the downloads: it stops
#: a nearly-complete cache from reading the whole catalogue every five minutes
#: to find the handful still missing.
SCAN_LIMIT = 20_000


def pending_count(db: Session) -> int:
    """Photos known of but not on disk, and still worth trying for."""
    return int(
        db.execute(
            select(func.count(CachedImage.id)).where(
                CachedImage.fetched_at.is_(None),
                CachedImage.gone.is_(False),
                CachedImage.attempts < 3,
            )
        ).scalar_one()
    )


def _settled(db: Session, urls: list[str]) -> set[str]:
    """Of these photos, the keys not worth fetching again.

    Either the file is on disk, or the shop has deleted it, or it has failed
    often enough to be counted out. Anything else is still owed. Skipping
    every *registered* photo, as this once did, skipped the entire backlog: a
    row is written the moment the crawler sees a photo, so after one pass
    every photo looked done and the prefetcher had nothing left to do. That is
    why the cache held about 200 MB against 131,716 known photos - the only
    ones ever downloaded were the ones somebody happened to open in a browser.
    """
    keys = list({key_for(url) for url in urls})
    if not keys:
        return set()
    return set(
        db.execute(
            select(CachedImage.key).where(
                CachedImage.key.in_(keys),
                or_(
                    CachedImage.fetched_at.is_not(None),
                    CachedImage.gone.is_(True),
                    CachedImage.attempts >= 3,
                ),
            )
        )
        .scalars()
        .all()
    )


def prefetch(db: Session, limit: int | None = None) -> dict:
    """Cache the photos of items that do not have theirs yet."""
    if not settings.image_cache_enabled:
        return {"fetched": 0, "skipped": 0, "reason": "disabled"}

    limit = limit or settings.image_prefetch_batch
    fetched = failed = scanned = 0

    # Walked in chunks so the "already have it" question is one query per
    # chunk rather than one per photo, and so a cache that is nearly complete
    # does not pay for a full catalogue read to find the last few gaps.
    chunk: list[str] = []
    for item_id in _priority_items(db):
        item = db.get(Item, item_id)
        if item is not None:
            chunk.extend(urls_for_item(item))
        if len(chunk) < 200:
            continue

        scanned += len(chunk)
        settled = _settled(db, chunk)
        for url in chunk:
            if key_for(url) in settled:
                continue
            if fetch(db, url, touch=False):
                fetched += 1
            else:
                failed += 1
            if fetched + failed >= limit:
                return _prefetch_result(db, fetched, failed, scanned)
        chunk = []
        if scanned >= SCAN_LIMIT:
            break

    if chunk:
        scanned += len(chunk)
        settled = _settled(db, chunk)
        for url in chunk:
            if key_for(url) in settled:
                continue
            if fetch(db, url, touch=False):
                fetched += 1
            else:
                failed += 1
            if fetched + failed >= limit:
                break

    return _prefetch_result(db, fetched, failed, scanned)


def _prefetch_result(db: Session, fetched: int, failed: int, scanned: int) -> dict:
    return {
        "fetched": fetched,
        "failed": failed,
        "scanned": scanned,
        "queued": pending_count(db),
    }


def prune(db: Session, budget_bytes: int | None = None) -> dict:
    """Drop the least recently shown images once past the budget.

    The rule that matters is replaceability. A thumbnail of something still on
    sale can be fetched again any time; the photo of a sold pre-owned listing
    cannot, because AmiAmi deleted the listing and this instance now holds the
    only copy. Those are protected outright, along with anything on a wishlist
    or under a watch, and eviction works through the replaceable images
    oldest-first until the cache is back inside its budget.

    If that is not enough on its own the cache stays over budget rather than
    destroying the irreplaceable half, and says so in the returned figures. A
    cache that is too small is a configuration problem; a cache that quietly
    ate the only record of a listing is not recoverable.
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
    # The ones the docstring is really about. Every pre-owned photograph is of
    # one particular second-hand copy - its actual wear, its actual box - not
    # a product shot, and AmiAmi deletes it when that copy sells. Protecting
    # only the ones already sold would be a race we lose by definition: the
    # photo has to survive the sale to be worth having, so it is protected
    # while the listing is still up.
    protected |= set(
        db.execute(
            select(Item.id).where(
                or_(Item.order_closed.is_(True), Item.condition == Condition.preowned)
            )
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
    # Materialised rather than streamed: rows are deleted inside the loop, and
    # mutating the session while iterating its own result is asking for
    # trouble on some drivers.
    candidates = list(
        db.execute(select(CachedImage).order_by(CachedImage.last_used_at.asc())).scalars()
    )

    kept_irreplaceable = 0
    for row in candidates:
        if total - freed <= budget:
            break
        if row.key in protected_keys:
            kept_irreplaceable += 1
            continue
        path = path_for(row.key, row.content_type)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("Could not remove cached image %s", row.key, exc_info=True)
            continue
        freed += row.bytes
        pruned += 1
        db.delete(row)

    db.commit()
    if total - freed > budget:
        log.warning(
            "Image cache is %.1f GB over its %.1f GB budget after pruning; "
            "%s protected image(s) were kept because they cannot be fetched again. "
            "Raise IMAGE_CACHE_MAX_GB if this persists.",
            (total - freed - budget) / 1024**3,
            budget / 1024**3,
            kept_irreplaceable,
        )
    if pruned:
        log.info("Pruned %s cached image(s), freeing %.0f MB", pruned, freed / 1024 / 1024)
    return {
        "pruned": pruned,
        "freed_bytes": freed,
        "total_bytes": total - freed,
        "budget_bytes": budget,
        "protected_kept": kept_irreplaceable,
        "over_budget": max(0, (total - freed) - budget),
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
    # A row is created the moment a photo is *seen*, without downloading it,
    # because the public route is a hash of the source URL and the server has
    # to know the mapping before it can fetch anything. So the row count is
    # how many photos we know of, and only the ones with a fetch time behind
    # them are actually on disk. Reporting the first as "cached" is how the
    # panel came to claim 131,716 photos in 200 MB, which is 1.5 kB each.
    downloaded = int(
        db.execute(
            select(func.count(CachedImage.id)).where(CachedImage.fetched_at.is_not(None))
        ).scalar_one()
    )
    items = int(db.execute(select(func.count(Item.id))).scalar_one())
    budget = int(settings.image_cache_max_gb * 1024**3)

    # How many photos the current catalogue would need in total.
    pending = pending_count(db)
    per_hour = int(
        min(
            settings.image_prefetch_batch * (60 / max(1, settings.image_prefetch_interval_minutes)),
            settings.image_cache_requests_per_minute * 60,
        )
    )
    per_item = 2 if settings.image_cache_full_images else 1
    expected = items * per_item
    # Averaged over what was actually downloaded, not over every row, or the
    # figure collapses towards zero as more photos are merely known about.
    average = (int(total_bytes) / downloaded) if downloaded else 0

    return {
        "enabled": settings.image_cache_enabled,
        "full_images": settings.image_cache_full_images,
        # Known: a URL we have recorded. Downloaded: a file on disk. The gap
        # between them is the prefetch backlog.
        "count": int(total_rows),
        "downloaded": downloaded,
        "pending": pending,
        "bytes": int(total_bytes),
        "budget_bytes": budget,
        "percent_of_budget": round(int(total_bytes) / budget * 100, 1) if budget else 0.0,
        "by_kind": by_kind,
        "gone_upstream": gone,
        "items_known": items,
        "expected_images": expected,
        "coverage_percent": round(downloaded / expected * 100, 1) if expected else 0.0,
        "known_percent": round(int(total_rows) / expected * 100, 1) if expected else 0.0,
        "average_bytes": int(average),
        "projected_bytes": int(average * expected) if average else 0,
        "requests_per_minute": settings.image_cache_requests_per_minute,
        # What the queue is actually draining at, so the wait can be quoted
        # rather than guessed. The token bucket caps it; the batch and the
        # interval decide whether it ever gets near that cap.
        "prefetch_per_hour": per_hour,
        "queue_hours": round(pending / per_hour, 1) if pending and per_hour else 0.0,
        "path": str(cache_root()),
    }
