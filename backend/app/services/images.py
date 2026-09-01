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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from . import reqlog
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

#: And which pictures are the extra ones - review shots and bonus contents -
#: rather than the photograph of the product itself.
#:
#: Named positively on purpose. The first version of this called anything not
#: under "/main/" a gallery shot, which is true of AmiAmi's own paths and
#: false of everything else: a product photo served from a path we have not
#: seen before would have been refused from the cache altogether, and the
#: item left with no picture at all. An unrecognised URL is kept.
GALLERY_MARKERS = ("/review/", "/bonus/")
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
    """Which of the three a photo is: thumbnail, full image, or gallery shot.

    AmiAmi serves the product photo from ``/main/`` and ``/thumb300/``, and
    the extra pictures - review shots, bonus contents - from paths of their
    own. Only the first two are worth keeping: they are one picture per item
    and the one that cannot be recovered once a used listing is deleted. The
    gallery can run to twenty-odd shots of a single figure, which is a
    different order of disk entirely, and while the item exists the shop will
    serve them itself.

    Told apart before this only as "thumbnail or not", which quietly filed
    every review shot as a full image - so the panel reported eighteen
    thousand more full images than thumbnails and nobody could see why.
    """
    if any(marker in url for marker in THUMB_MARKERS):
        return "thumb"
    if any(marker in url for marker in GALLERY_MARKERS):
        return "gallery"
    return "main"


def path_for(key: str, content_type: str = "image/jpeg") -> Path:
    """Sharded so no single directory ends up with a hundred thousand files."""
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(content_type, ".jpg")
    return cache_root() / key[:2] / key[2:4] / f"{key}{extension}"


def already_cached(db: Session, urls: list[str]) -> set[str]:
    """Which of these we hold a downloaded copy of. One query, not one each."""
    wanted = {key_for(u): u for u in urls if u}
    if not wanted:
        return set()
    keys = (
        db.execute(
            select(CachedImage.key).where(
                CachedImage.key.in_(wanted),
                CachedImage.fetched_at.is_not(None),
                CachedImage.gone.is_(False),
            )
        )
        .scalars()
        .all()
    )
    return {wanted[key] for key in keys}


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
    # Counted against the image host, not against the API. They are separate
    # servers with separate allowances, and lumping them together made the
    # panel report 116% of a 24/min budget that photos were never drawing on.
    reqlog.record(
        "amiami-images",
        ok=response.status_code < 400,
        name="images",
        url=url,
        status=response.status_code,
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


def register(
    db: Session,
    urls: list[str],
    *,
    item_id: int | None = None,
    commit: bool = False,
) -> int:
    """Note that these photos exist, without downloading them.

    The public route is a hash of the source URL, which cannot be reversed, so
    the server has to have been told the mapping before it can serve or fetch
    anything. Recording it when the item is stored means one write per photo
    ever, rather than a lookup on every page render.
    """
    if not settings.image_cache_enabled:
        return 0

    # Gallery shots are not kept. A row here is a promise to fetch the photo
    # eventually, so recording one is the same as deciding to spend the disk;
    # the single choke point is the honest place to refuse, because every
    # route into the cache runs through it. The item page shows those
    # pictures straight from the shop instead.
    wanted = {
        key_for(u): u
        for u in urls
        if u and u.startswith("http") and kind_for(u) != "gallery"
    }
    if not wanted:
        return 0

    known = set(
        db.execute(select(CachedImage.key).where(CachedImage.key.in_(wanted))).scalars().all()
    )
    added = 0
    for key, url in wanted.items():
        if key in known:
            continue
        db.add(
            CachedImage(key=key, source_url=url, kind=kind_for(url), item_id=item_id)
        )
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


def _pending_queue(db: Session, limit: int):
    """Photos still owed, worst-to-lose first.

    One query, ordered, straight at the work. The version before this walked
    every item in priority order and asked, item by item, whether its photos
    were already on disk - which meant re-walking the whole cached prefix on
    every run. It had a scan limit to bound that, and once the prefix grew
    past the limit the walk never reached an outstanding photo again: measured
    on forty thousand items, it fetched fifty at forty per cent coverage and
    exactly zero from fifty per cent onwards. The queue had not slowed down,
    it had stopped.

    The order is by what is lost if we are too slow. A pre-owned listing is
    one copy: when it sells, AmiAmi takes down the photographs of that copy,
    and they were the only pictures of the actual item rather than of the
    product. A new item's stock photo stays up as long as the product does.
    """
    watched = (
        select(WatchSeenItem.item_id)
        .join(Watch, Watch.id == WatchSeenItem.watch_id)
        .where(Watch.enabled.is_(True))
    )
    priority = case(
        # Asked for by name. Losing one of these costs someone something.
        (CachedImage.item_id.in_(select(CollectionEntry.item_id)), 0),
        (CachedImage.item_id.in_(watched), 1),
        # Used copies on sale now: the irreplaceable ones.
        (and_(Item.in_stock.is_(True), Item.condition == Condition.preowned), 2),
        # Anything else buyable, whose photos usually return with a restock.
        (Item.in_stock.is_(True), 3),
        else_=4,
    )
    return list(
        db.execute(
            select(CachedImage)
            # Outer, so a photo recorded before items were linked - or one
            # whose item has since gone - is still reachable, just last.
            .outerjoin(Item, Item.id == CachedImage.item_id)
            .where(
                CachedImage.fetched_at.is_(None),
                CachedImage.gone.is_(False),
                CachedImage.attempts < 3,
                # Rows left over from when these were kept. Nothing registers
                # one now, but the ones already recorded would still be
                # fetched, and a single figure can carry twenty-odd of them -
                # which is why a batch could run photo 13 through 26 of one
                # product while other items had no picture at all.
                CachedImage.kind != "gallery",
            )
            .order_by(priority, Item.first_seen_at.desc().nulls_last(), CachedImage.id)
            .limit(limit)
        )
        .scalars()
        .all()
    )


def pending_count(db: Session) -> int:
    """Photos known of but not on disk, and still worth trying for.

    Counted over exactly what the queue will take, so the wait quoted from it
    is a wait for work that will actually happen.
    """
    return int(
        db.execute(
            select(func.count(CachedImage.id)).where(
                CachedImage.fetched_at.is_(None),
                CachedImage.gone.is_(False),
                CachedImage.attempts < 3,
                CachedImage.kind != "gallery",
            )
        ).scalar_one()
    )


def download_rate(db: Session, hours: int = 24) -> float:
    """Photos an hour actually landing on disk, measured.

    Not derived from the batch size and the interval: those say what the job
    is permitted, and the gap between that and what happens is the whole
    reason anyone looks. A run can fetch nothing because there is nothing
    left, because the shop refused, or because the queue could not be
    reached - and only the measurement notices.
    """
    since = utcnow() - timedelta(hours=hours)
    done = int(
        db.execute(
            select(func.count(CachedImage.id)).where(CachedImage.fetched_at >= since)
        ).scalar_one()
    )
    if not done:
        return 0.0
    # Against the window, not against the age of the oldest one in it: a cache
    # that filled in an hour and then stopped should read as stopped.
    return round(done / hours, 1)


def prefetch(db: Session, limit: int | None = None) -> dict:
    """Cache the photos of items that do not have theirs yet."""
    if not settings.image_cache_enabled:
        return {"fetched": 0, "skipped": 0, "reason": "disabled"}

    limit = limit or settings.image_prefetch_batch
    fetched = failed = 0
    queue = _pending_queue(db, limit)
    for index, row in enumerate(queue, start=1):
        reqlog.doing(
            "images",
            f"photo {index} of {len(queue)} in this batch",
            queued=pending_count(db) if index == 1 else None,
            source=row.source_url,
            kind=row.kind,
        )
        if fetch(db, row.source_url, touch=False):
            fetched += 1
        else:
            failed += 1
    reqlog.done("images")
    return _prefetch_result(db, fetched, failed)


def _prefetch_result(db: Session, fetched: int, failed: int) -> dict:
    return {
        "fetched": fetched,
        "failed": failed,
        "queued": pending_count(db),
        "per_hour": download_rate(db),
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
    # Items that have a photograph at all. Counting every row overstated the
    # target by the several thousand the shop never pictured, which made the
    # coverage percentage permanently unreachable.
    items = int(
        db.execute(
            select(func.count(Item.id)).where(
                or_(Item.image_url.is_not(None), Item.images != [])
            )
        ).scalar_one()
    )
    budget = int(settings.image_cache_max_gb * 1024**3)

    # How many photos the current catalogue would need in total.
    pending = pending_count(db)
    measured = download_rate(db)
    per_hour = int(
        min(
            settings.image_prefetch_batch * (60 / max(1, settings.image_prefetch_interval_minutes)),
            settings.image_cache_requests_per_minute * 60,
        )
    )
    per_item = 2 if settings.image_cache_full_images else 1
    expected = items * per_item

    # Gallery shots are not kept, so they are no part of the target. Rows for
    # them exist from before that was decided; they are reported on their own
    # rather than counted as full images, which is what made the panel show
    # eighteen thousand more full images than thumbnails and a bar reading
    # 148,058 of 143,102 - more downloaded than there was to download.
    gallery = by_kind.pop("gallery", {"count": 0, "bytes": 0})
    kept_rows = int(total_rows) - gallery["count"]
    # A row is created the moment a photo is *seen*, without downloading it,
    # because the public route is a hash of the source URL and the server has
    # to know the mapping before it can fetch anything. So the row count is
    # how many photos we know of, and only the ones with a fetch time behind
    # them are actually on disk. Reporting the first as "cached" is how the
    # panel came to claim 131,716 photos in 200 MB, which is 1.5 kB each.
    kept_downloaded = int(
        db.execute(
            select(func.count(CachedImage.id)).where(
                CachedImage.fetched_at.is_not(None), CachedImage.kind != "gallery"
            )
        ).scalar_one()
    )
    # Averaged over what was actually downloaded, not over every row, or the
    # figure collapses towards zero as more photos are merely known about.
    # And over the kept photos only: a review shot is a full-size picture, so
    # letting them into the average makes the projection for a cache that no
    # longer holds any of them too large.
    kept_bytes = int(total_bytes) - gallery["bytes"]
    average = (kept_bytes / kept_downloaded) if kept_downloaded else 0

    return {
        "enabled": settings.image_cache_enabled,
        "full_images": settings.image_cache_full_images,
        # Known: a URL we have recorded. Downloaded: a file on disk. The gap
        # between them is the prefetch backlog.
        "count": kept_rows,
        "downloaded": kept_downloaded,
        "pending": pending,
        "bytes": int(total_bytes),
        "budget_bytes": budget,
        "percent_of_budget": round(int(total_bytes) / budget * 100, 1) if budget else 0.0,
        "by_kind": by_kind,
        "gone_upstream": gone,
        "items_known": items,
        "expected_images": expected,
        # Extra pictures kept from before they stopped being cached. Held on
        # to rather than deleted: some belong to listings the shop has since
        # removed, and those cannot be fetched again from anywhere.
        "gallery_kept": gallery["count"],
        "gallery_bytes": gallery["bytes"],
        "coverage_percent": (
            round(min(kept_downloaded, expected) / expected * 100, 1) if expected else 0.0
        ),
        "known_percent": (
            round(min(kept_rows, expected) / expected * 100, 1) if expected else 0.0
        ),
        "average_bytes": int(average),
        "projected_bytes": int(average * expected) if average else 0,
        "requests_per_minute": settings.image_cache_requests_per_minute,
        # What the queue is actually draining at, so the wait can be quoted
        # rather than guessed. The token bucket caps it; the batch and the
        # interval decide whether it ever gets near that cap.
        # What the schedule permits, and what has actually been landing. The
        # second is the one to quote a wait from: a queue the prefetcher
        # cannot reach drains at nothing however generous the settings are.
        "prefetch_per_hour": per_hour,
        "measured_per_hour": measured,
        "queue_hours": (
            round(pending / measured, 1)
            if pending and measured
            else (round(pending / per_hour, 1) if pending and per_hour else 0.0)
        ),
        "queue_measured": bool(pending and measured),
        "path": str(cache_root()),
    }
