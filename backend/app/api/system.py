"""Public config, health, status and administration."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..deps import admin_user, current_user
from ..models import (
    Alert,
    Item,
    NotificationChannel,
    PricePoint,
    User,
    UserRole,
    Watch,
)
from ..notifiers import describe_all
from ..providers import all_providers
from ..schemas import MessageResponse, PublicConfig, SystemStatus, UserOut
from ..services import dealradar, fx
from .search import provider_info

router = APIRouter(tags=["system"])

VERSION = "1.0.0"


#: Uploaded archives can be gigabytes, so they are spooled to disk in chunks
#: rather than read into memory in one go.
UPLOAD_CHUNK = 4 * 1024 * 1024


async def _spool_upload(file: UploadFile, workdir: Path) -> Path:
    """Write an upload to disk without holding it all in memory at once."""
    target = workdir / "upload.zip"
    with target.open("wb") as out:
        while chunk := await file.read(UPLOAD_CHUNK):
            out.write(chunk)
    if target.stat().st_size == 0:
        raise ValueError("The uploaded file is empty")
    return target

_STARTED_AT = time.time()


@router.get("/config", response_model=PublicConfig)
def public_config(db: Session = Depends(get_db)) -> PublicConfig:
    """Everything the login screen needs before anyone is authenticated."""
    has_users = bool(db.execute(select(func.count(User.id))).scalar_one())
    return PublicConfig(
        app_name=settings.app_name,
        registration_open=settings.allow_registration or not has_users,
        has_users=has_users,
        vapid_public_key=settings.vapid_public_key or None,
        providers=[provider_info(p) for p in all_providers()],
        channel_types=describe_all(),
        default_currency=settings.display_currency,
        min_poll_interval_seconds=settings.min_poll_interval_seconds,
        version=VERSION,
        refresh_on_open=bool(behaviour(db).get("refresh_on_open")),
    )


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "version": VERSION}


@router.get("/status", response_model=SystemStatus)
def system_status(
    db: Session = Depends(get_db), _user: User = Depends(current_user)
) -> SystemStatus:
    from ..scheduler.engine import engine

    def count(model) -> int:
        return int(db.execute(select(func.count(model.id))).scalar_one() or 0)

    return SystemStatus(
        version=VERSION,
        scheduler=engine.status(),
        providers=[provider_info(p) for p in all_providers()],
        fx=fx.snapshot(db),
        database=settings.resolved_database_url.split("://", 1)[0],
        users=count(User),
        watches=count(Watch),
        items=count(Item),
        price_points=count(PricePoint),
        alerts=count(Alert),
        data_dir=str(settings.data_dir),
        webpush_configured=bool(settings.vapid_public_key and settings.vapid_private_key),
        smtp_configured=bool(settings.smtp_host),
        registration_open=settings.allow_registration,
        uptime_seconds=round(time.time() - _STARTED_AT, 1),
    )


@router.post("/fx/refresh", response_model=MessageResponse)
def refresh_fx(db: Session = Depends(get_db), _user: User = Depends(current_user)) -> MessageResponse:
    written = fx.refresh_rates(db)
    if not written:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Every exchange rate source failed"
        )
    return MessageResponse(message=f"Refreshed {written} rate(s)", detail=fx.snapshot(db))


@router.post("/deal-radar/scan", response_model=MessageResponse)
def scan_deals(db: Session = Depends(get_db), user: User = Depends(current_user)) -> MessageResponse:
    found = dealradar.scan(db, user_id=user.id)
    return MessageResponse(message=f"Deal radar raised {found} alert(s)", detail={"alerts": found})


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

admin = APIRouter(prefix="/admin", tags=["admin"])


@admin.get("/users", response_model=list[dict])
def list_users(db: Session = Depends(get_db), _admin: User = Depends(admin_user)) -> list[dict]:
    users = db.execute(select(User).order_by(User.created_at)).scalars()
    out = []
    for user in users:
        out.append(
            {
                **UserOut.model_validate(user).model_dump(mode="json"),
                "watch_count": int(
                    db.execute(
                        select(func.count(Watch.id)).where(Watch.user_id == user.id)
                    ).scalar_one()
                    or 0
                ),
                "channel_count": int(
                    db.execute(
                        select(func.count(NotificationChannel.id)).where(
                            NotificationChannel.user_id == user.id
                        )
                    ).scalar_one()
                    or 0
                ),
                "alert_count": int(
                    db.execute(
                        select(func.count(Alert.id)).where(Alert.user_id == user.id)
                    ).scalar_one()
                    or 0
                ),
            }
        )
    return out


@admin.patch("/users/{user_id}", response_model=MessageResponse)
def update_user(
    user_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    acting_admin: User = Depends(admin_user),
) -> MessageResponse:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if "is_active" in payload:
        if target.id == acting_admin.id and not payload["is_active"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot deactivate your own account",
            )
        target.is_active = bool(payload["is_active"])

    if "role" in payload:
        try:
            new_role = UserRole(payload["role"])
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown role"
            ) from exc
        if target.id == acting_admin.id and new_role != UserRole.admin:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot remove your own administrator role",
            )
        admins_left = int(
            db.execute(
                select(func.count(User.id)).where(
                    User.role == UserRole.admin, User.id != target.id
                )
            ).scalar_one()
            or 0
        )
        if new_role != UserRole.admin and admins_left == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This instance needs at least one administrator",
            )
        target.role = new_role

    db.commit()
    return MessageResponse(message="User updated")


@admin.delete("/users/{user_id}", response_model=MessageResponse)
def delete_user(
    user_id: int, db: Session = Depends(get_db), acting_admin: User = Depends(admin_user)
) -> MessageResponse:
    if user_id == acting_admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own account"
        )
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    db.delete(target)
    db.commit()
    return MessageResponse(message="User and all their data deleted")


@admin.get("/catalog", response_model=MessageResponse)
def catalog_progress(
    provider: str = "amiami",
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> MessageResponse:
    """How far the background catalogue build and MFC linking have got."""
    from ..services import crawler, enrich

    detail = crawler.progress(db, provider)
    detail["mfc"] = enrich.tag_stats(db)
    detail["mfc"]["enabled"] = settings.mfc_enabled
    detail["mfc"]["requests_per_minute"] = settings.mfc_requests_per_minute
    detail["mfc"]["eta_seconds"] = enrich.eta_seconds(db)
    return MessageResponse(message="ok", detail=detail)


@admin.post("/catalog/run", response_model=MessageResponse)
def run_catalog_crawl(
    seconds: int = Query(default=30, ge=5, le=120),
    provider: str = "amiami",
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> MessageResponse:
    """Crawl for a few seconds right now, so progress is visible immediately."""
    from ..services import crawler

    outcome = crawler.run_once(db, provider, budget_seconds=seconds)
    return MessageResponse(
        message=(
            f"{outcome.pages} page(s), {outcome.items} item(s), "
            f"{outcome.new_items} new. Stopped: {outcome.stopped_because or 'budget spent'}"
        ),
        detail=outcome.as_dict(),
    )


@admin.get("/activity", response_model=MessageResponse)
def shop_activity(
    days: int = Query(default=30, ge=1, le=365),
    provider: str = "amiami",
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> MessageResponse:
    """When new listings and price changes actually arrive.

    So the polling schedule can follow the shop's rhythm instead of hitting it
    uniformly around the clock.
    """
    from ..services import crawler

    return MessageResponse(
        message="ok", detail=crawler.activity_profile(db, provider, days=days)
    )


@admin.get("/shelf-life", response_model=MessageResponse)
def shelf_coverage(
    provider: str = "amiami",
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> MessageResponse:
    """How much of the pre-owned catalogue is being followed, and how closely."""
    from ..services import shelfwatch

    return MessageResponse(message="ok", detail=shelfwatch.coverage(db, provider))


@admin.post("/shelf-life/run", response_model=MessageResponse)
def run_shelf_sampler(
    seconds: int = Query(default=30, ge=5, le=120),
    provider: str = "amiami",
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> MessageResponse:
    """Sample a few products right now, so the effect is visible immediately."""
    from ..services import shelfwatch

    outcome = shelfwatch.run_once(db, provider, budget_seconds=seconds)
    return MessageResponse(
        message=(
            f"{outcome.checked} product(s) checked, {outcome.appeared} copy(ies) appeared, "
            f"{outcome.vanished} sold, {outcome.delisted} product(s) gone. "
            f"Stopped: {outcome.stopped_because or 'budget spent'}"
        ),
        detail=outcome.as_dict(),
    )


@admin.patch("/catalog/{scope}", response_model=MessageResponse)
def update_catalog_slice(
    scope: str,
    payload: dict,
    provider: str = "amiami",
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> MessageResponse:
    """Enable, disable or rewind one slice of the catalogue build."""
    from ..models import CatalogCrawl, CrawlState

    crawl = db.execute(
        select(CatalogCrawl).where(
            CatalogCrawl.provider == provider, CatalogCrawl.scope == scope
        )
    ).scalar_one_or_none()
    if crawl is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown slice")

    if "enabled" in payload:
        crawl.enabled = bool(payload["enabled"])
    if payload.get("restart"):
        crawl.cursor_page = 1
        crawl.state = CrawlState.idle
        crawl.consecutive_errors = 0
        crawl.last_error = None
        crawl.finished_at = None

    # How often this slice re-reads the shop's newest pages, and how deep.
    if "recheck_interval_minutes" in payload:
        crawl.recheck_interval_minutes = max(
            5, min(int(payload["recheck_interval_minutes"]), 60 * 24 * 7)
        )
    if "head_pages" in payload:
        crawl.head_pages = max(1, min(int(payload["head_pages"]), 500))
    if "full_sweep_interval_days" in payload:
        crawl.full_sweep_interval_days = max(1, min(int(payload["full_sweep_interval_days"]), 365))
    if "priority" in payload:
        crawl.priority = max(0, min(int(payload["priority"]), 1000))

    db.commit()
    return MessageResponse(message=f"Slice {scope} updated")


@admin.get("/images", response_model=MessageResponse)
def image_cache_status(
    db: Session = Depends(get_db), _admin: User = Depends(admin_user)
) -> MessageResponse:
    """How much disk the cached product photos take, and how far they reach."""
    from ..services import images as image_cache

    return MessageResponse(message="ok", detail=image_cache.stats(db))


@admin.post("/images/prefetch", response_model=MessageResponse)
def prefetch_images(
    limit: int = Query(default=40, ge=1, le=500),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> MessageResponse:
    """Cache another batch now rather than waiting for the background job."""
    from ..services import images as image_cache

    outcome = image_cache.prefetch(db, limit=limit)
    return MessageResponse(
        message=f"Cached {outcome.get('fetched', 0)} photo(s), {outcome.get('failed', 0)} unavailable",
        detail=outcome,
    )


@admin.post("/images/prune", response_model=MessageResponse)
def prune_images(
    db: Session = Depends(get_db), _admin: User = Depends(admin_user)
) -> MessageResponse:
    """Drop the least recently shown photos back under the budget."""
    from ..services import images as image_cache

    outcome = image_cache.prune(db)
    return MessageResponse(
        message=(
            f"Removed {outcome['pruned']} photo(s), freeing "
            f"{outcome['freed_bytes'] / 1024 / 1024:.0f} MB"
        ),
        detail=outcome,
    )


@admin.get("/health", response_model=MessageResponse)
def health_report(
    db: Session = Depends(get_db), _admin: User = Depends(admin_user)
) -> MessageResponse:
    """What is currently wrong with the background machinery, if anything."""
    from ..services import health

    # Read-only: looking at the page must not fire notifications.
    report = health.check(db, notify_enabled=False)
    return MessageResponse(
        ok=report["healthy"],
        message="Everything is working" if report["healthy"] else
        f"{len(report['issues'])} problem(s) need attention",
        detail=report,
    )


@admin.post("/health/test", response_model=MessageResponse)
def health_test(
    db: Session = Depends(get_db), _admin: User = Depends(admin_user)
) -> MessageResponse:
    """Send a sample health alert, to prove the warning path works.

    Worth doing once: an alert about the notifications being broken is only
    useful if it can actually get out.
    """
    from ..services.health import Issue, _deliver

    sample = Issue(
        key="test",
        title="Test alert from AmiSearch",
        detail="This is what a problem with the background scraping would look like.",
        hint="If you can read this, health alerts will reach you.",
    )
    delivered = _deliver(db, sample, resolved=False)
    if not delivered:
        return MessageResponse(
            ok=False,
            message="No usable channel. Add one under Settings, Notifications first.",
        )
    return MessageResponse(message=f"Sent to {delivered} channel(s)")


#: Key under which the runtime MyFigureCollection session is stored, so it can
#: be changed without a restart. It is never echoed back to the browser.
MFC_SESSION_SETTING = "mfc_session"

#: Instance-wide behaviour toggles kept in the database so they survive a
#: restart and can be changed without touching the environment.
BEHAVIOUR_SETTING = "behaviour"
BEHAVIOUR_DEFAULTS = {"refresh_on_open": False}


def behaviour(db: Session) -> dict:
    """Instance behaviour flags, with defaults filled in."""
    from ..models import AppSetting

    row = db.get(AppSetting, BEHAVIOUR_SETTING)
    return {**BEHAVIOUR_DEFAULTS, **((row.value if row else None) or {})}


@admin.get("/behaviour", response_model=MessageResponse)
def get_behaviour(
    db: Session = Depends(get_db), _admin: User = Depends(admin_user)
) -> MessageResponse:
    return MessageResponse(message="ok", detail=behaviour(db))


@admin.put("/behaviour", response_model=MessageResponse)
def set_behaviour(
    payload: dict, db: Session = Depends(get_db), _admin: User = Depends(admin_user)
) -> MessageResponse:
    """Change how the instance behaves for everyone using it.

    Refreshing on open costs one upstream request per item anyone views, taken
    from the same budget the crawler and the watches share, so it is an
    instance decision rather than a personal preference.
    """
    from ..models import AppSetting

    row = db.get(AppSetting, BEHAVIOUR_SETTING)
    if row is None:
        row = AppSetting(key=BEHAVIOUR_SETTING, value={})
        db.add(row)
    merged = {**behaviour(db)}
    for key in BEHAVIOUR_DEFAULTS:
        if key in payload:
            merged[key] = bool(payload[key])
    row.value = merged
    db.commit()
    return MessageResponse(message="Saved", detail=merged)


def load_mfc_session(db: Session) -> None:
    """Apply a stored session cookie to the client at start-up."""
    from ..enrichment.mfc import client
    from ..models import AppSetting

    row = db.get(AppSetting, MFC_SESSION_SETTING)
    if row and (row.value or {}).get("cookie"):
        client.set_session_cookie(row.value["cookie"])


@admin.get("/mfc/session", response_model=MessageResponse)
def mfc_session_status(_admin: User = Depends(admin_user)) -> MessageResponse:
    """Whether a signed-in MyFigureCollection session is configured and working."""
    from ..enrichment.mfc import client

    return MessageResponse(message="ok", detail=client.check_session())


@admin.put("/mfc/session", response_model=MessageResponse)
def set_mfc_session(
    payload: dict, db: Session = Depends(get_db), _admin: User = Depends(admin_user)
) -> MessageResponse:
    """Store a PHPSESSID copied from a signed-in browser.

    A cookie rather than a password: the account password never reaches this
    database, and signing out on MyFigureCollection revokes access at once.
    """
    from ..enrichment.mfc import client
    from ..models import AppSetting

    cookie = str(payload.get("cookie") or "").strip()
    row = db.get(AppSetting, MFC_SESSION_SETTING)
    if row is None:
        row = AppSetting(key=MFC_SESSION_SETTING, value={})
        db.add(row)
    row.value = {"cookie": cookie}
    db.commit()

    client.set_session_cookie(cookie)
    if not cookie:
        return MessageResponse(message="Session cleared. Restricted entries stay unreadable.")

    result = client.check_session()
    return MessageResponse(
        ok=bool(result.get("valid")),
        message=result.get("detail", ""),
        detail=result,
    )


@admin.post("/mfc/recheck-restricted", response_model=MessageResponse)
def recheck_restricted(
    limit: int = Query(default=25, ge=1, le=200),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> MessageResponse:
    """Re-read entries that were withheld before a session was configured."""
    from ..models import Item
    from ..services import enrich

    items = list(
        db.execute(
            select(Item).where(Item.mfc_restricted.is_(True)).limit(limit)
        )
        .scalars()
        .all()
    )
    linked = sum(1 for item in items if enrich.enrich_item(db, item, force=True))
    return MessageResponse(
        message=f"Re-read {len(items)} withheld entr(ies), {linked} now carry tags",
        detail={"checked": len(items), "tagged": linked},
    )


@admin.get("/settings", response_model=MessageResponse)
def instance_settings(_admin: User = Depends(admin_user)) -> MessageResponse:
    """Read-only view of the environment configuration, secrets omitted."""
    return MessageResponse(
        message="ok",
        detail={
            "allow_registration": settings.allow_registration,
            "scheduler_enabled": settings.scheduler_enabled,
            "worker_concurrency": settings.worker_concurrency,
            "min_poll_interval_seconds": settings.min_poll_interval_seconds,
            "default_poll_interval_seconds": settings.default_poll_interval_seconds,
            "adaptive_polling": settings.adaptive_polling,
            "provider_requests_per_minute": settings.provider_requests_per_minute,
            "provider_max_concurrency": settings.provider_max_concurrency,
            "fx_refresh_hours": settings.fx_refresh_hours,
            "display_currency": settings.display_currency,
            "price_history_retention_days": settings.price_history_retention_days,
            "alert_retention_days": settings.alert_retention_days,
            "base_url": settings.base_url,
            "smtp_host": settings.smtp_host,
            "smtp_configured": bool(settings.smtp_host),
            "webpush_configured": bool(settings.vapid_public_key),
        },
    )


@admin.post("/vapid/generate", response_model=MessageResponse)
def generate_vapid(_admin: User = Depends(admin_user)) -> MessageResponse:
    """Mint a VAPID keypair. The values still have to be put in the env file."""
    from ..notifiers import generate_vapid_keys

    keys = generate_vapid_keys()
    return MessageResponse(
        message="Add these to your environment, then restart the container",
        detail={
            "VAPID_PUBLIC_KEY": keys["public_key"],
            "VAPID_PRIVATE_KEY": keys["private_key_der"],
        },
    )


@admin.get("/backup")
def download_backup(
    background: BackgroundTasks,
    include_images: bool = Query(default=False),
    include_secrets: bool = Query(default=True),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
):
    """Download the whole instance as a zip.

    Without images this is small and quick: the database is where the years of
    price history and every deleted listing actually live. With images it is
    the full picture and can run to gigabytes, since the photos of sold
    pre-owned listings exist nowhere else once AmiAmi drops them.

    The archive is built on disk rather than in memory and removed once the
    download has been sent.
    """
    from ..services import backup

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    suffix = "full" if include_images else "db"
    workdir = Path(tempfile.mkdtemp(prefix="amisearch-backup-", dir=str(settings.data_dir)))
    target = workdir / f"amisearch-{suffix}-{stamp}.zip"

    try:
        backup.create_archive(
            db, target, include_images=include_images, include_secrets=include_secrets
        )
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not build the backup: {exc}",
        ) from exc

    background.add_task(shutil.rmtree, workdir, ignore_errors=True)
    return FileResponse(
        target,
        media_type="application/zip",
        filename=target.name,
        background=background,
    )


@admin.post("/backup/inspect", response_model=MessageResponse)
async def inspect_backup(
    file: UploadFile = File(...),
    _admin: User = Depends(admin_user),
) -> MessageResponse:
    """Read an uploaded archive's manifest without applying any of it.

    So the confirmation step can say what is actually in the file - when it
    was taken, how big the database is, whether photos are included - rather
    than asking someone to overwrite their instance on faith.
    """
    from ..services import backup

    workdir = Path(tempfile.mkdtemp(prefix="amisearch-inspect-", dir=str(settings.data_dir)))
    try:
        staged = await _spool_upload(file, workdir)
        manifest = backup.inspect_archive(staged)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return MessageResponse(message="Archive looks readable", detail=manifest)


@admin.post("/restore", response_model=MessageResponse)
async def restore_backup(
    file: UploadFile = File(...),
    restore_images: bool = Query(default=True),
    _admin: User = Depends(admin_user),
) -> MessageResponse:
    """Replace this instance's data with an uploaded archive.

    Background jobs are held still for the duration, and the database being
    replaced is renamed rather than deleted, so it is still on disk afterwards
    if the restore turns out to be the wrong one.
    """
    from ..scheduler.engine import engine as polling_engine
    from ..services import backup

    workdir = Path(tempfile.mkdtemp(prefix="amisearch-restore-", dir=str(settings.data_dir)))
    try:
        staged = await _spool_upload(file, workdir)
        with polling_engine.paused():
            result = backup.restore_archive(staged, restore_images=restore_images)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    taken = result.manifest.get("created_at", "an unknown date")
    return MessageResponse(
        message=(
            f"Restored the backup taken {taken}. "
            f"{result.images_restored} image(s) written, "
            f"{len(result.migrated_columns)} column(s) migrated. "
            f"Your previous database was kept as {result.previous_database or 'nothing'}."
        ),
        detail=result.as_dict(),
    )


@admin.get("/config/export")
def export_configuration(
    include_secrets: bool = Query(default=False),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
):
    """Download the settings on their own, for setting up a second instance.

    Cost profiles, notification channels and crawl tuning are fiddly to redo by
    hand and tiny to carry. Channels hold bot tokens and webhook URLs in plain
    text, so they only travel when explicitly asked for.
    """
    from ..services import backup

    payload = backup.export_config(db, include_secrets=include_secrets)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return JSONResponse(
        content=jsonable_encoder(payload),
        headers={
            "Content-Disposition": f'attachment; filename="amisearch-config-{stamp}.json"'
        },
    )


@admin.post("/config/import", response_model=MessageResponse)
async def import_configuration(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    _admin: User = Depends(admin_user),
) -> MessageResponse:
    """Apply an exported settings file to this instance.

    Only ever adds or updates. Anything it cannot match - a user who does not
    exist here, a channel exported without its credentials - is reported back
    rather than being papered over.
    """
    from ..services import backup

    raw = await file.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That file is not readable JSON",
        ) from exc

    try:
        result = backup.import_config(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    parts = [
        f"{result.app_settings} setting(s)",
        f"{result.cost_profiles} cost profile(s)",
        f"{result.channels} channel(s)",
        f"{result.crawl_slices} crawl slice(s)",
    ]
    message = "Applied " + ", ".join(parts) + "."
    if result.skipped:
        message += f" {len(result.skipped)} item(s) skipped."
    return MessageResponse(message=message, detail=result.as_dict())
