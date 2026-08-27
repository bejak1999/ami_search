"""Serving product photos from the local cache."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Response
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import CachedImage
from ..services import images as image_cache

log = logging.getLogger(__name__)
router = APIRouter(prefix="/images", tags=["images"])

#: A 1x1 transparent GIF, so a missing photo leaves a clean empty frame rather
#: than the browser's broken-image icon.
_BLANK = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c000000000100010000020144003b"
)


@router.get("/{key}")
def serve_image(
    key: str, background: BackgroundTasks, db: Session = Depends(get_db)
) -> Response:
    """Return a cached photo, fetching it on first sight.

    Deliberately unauthenticated. These are public product photos from a shop,
    the key reveals nothing, and requiring a session here would stop the
    browser caching them and break image loading in the notification e-mails.
    """
    row = db.execute(select(CachedImage).where(CachedImage.key == key)).scalar_one_or_none()

    if row is not None and row.fetched_at and not row.gone:
        path = image_cache.path_for(key, row.content_type)
        if path.is_file():
            row.last_used_at = image_cache.utcnow()
            row.use_count += 1
            db.commit()
            return FileResponse(
                path,
                media_type=row.content_type,
                headers={
                    # Content-addressed by URL hash, so it can never go stale.
                    "Cache-Control": "public, max-age=31536000, immutable",
                },
            )

    if row is None:
        # Never registered, so the source URL is unknown and unrecoverable:
        # the route is a hash. Must not be cached, or the blank frame sticks
        # around after the item is registered on a later render.
        return Response(
            content=_BLANK,
            media_type="image/gif",
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    if row.gone:
        # The listing sold and the shop removed its pictures. Everything else
        # about the figure is still here; only the photo is lost.
        return Response(
            content=_BLANK,
            media_type="image/gif",
            status_code=410,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # Not cached yet. Downloading it here would make a page of forty-eight
    # tiles wait on forty-eight sequential fetches, so send the browser
    # straight to the origin and pull the copy in behind it. The next view is
    # served locally, and this one is not blank.
    background.add_task(_cache_later, key)
    return RedirectResponse(
        row.source_url,
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


def _cache_later(key: str) -> None:
    """Fetch one image outside the request, on its own session."""
    from ..db import session_scope
    from ..models import CachedImage as Row

    try:
        with session_scope() as db:
            row = db.execute(select(Row).where(Row.key == key)).scalar_one_or_none()
            if row is not None and not row.gone and not row.fetched_at:
                image_cache.fetch(db, row.source_url, touch=False)
    except Exception:  # noqa: BLE001 - a failed image must never surface
        log.debug("Background image fetch failed for %s", key, exc_info=True)
