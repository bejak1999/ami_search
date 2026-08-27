"""Taking the whole instance with you, and putting it back.

Three things are worth carrying between machines, and they have very
different sizes and risks:

* **the database** - years of price history, every listing the shop has since
  deleted, and everyone's watches. Small, irreplaceable.
* **the photos** - gigabytes of them, and the ones belonging to deleted
  listings cannot be fetched again from anywhere.
* **the settings** - cost profiles, notification channels, crawl tuning. Tiny,
  fiddly to redo by hand, and full of secrets.

So a backup is a zip holding the first two, and settings travel either inside
it or on their own as JSON, because moving to a new host usually means a fresh
database but the same configuration.

Restoring is the dangerous direction, and everything here is arranged around
one rule: never destroy the running instance until the replacement has been
proved good. The uploaded archive is unpacked to a staging directory, the
database inside it is opened and integrity checked, the outgoing database is
copied aside under its own name, and only then is anything overwritten.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    AppSetting,
    CatalogCrawl,
    ChannelType,
    CostProfile,
    NotificationChannel,
    User,
)

log = logging.getLogger(__name__)

#: Bumped when the archive layout changes in a way older code cannot read.
ARCHIVE_FORMAT = 1

DB_ENTRY = "database/amisearch.db"
CONFIG_ENTRY = "config.json"
MANIFEST_ENTRY = "manifest.json"
IMAGE_PREFIX = "images/"

#: Tables an archive must contain to be plausibly one of ours. Deliberately a
#: minimum rather than the full list, so a backup from an older version with
#: fewer tables still restores and is then migrated forward.
REQUIRED_TABLES = {"users", "items", "price_points", "watches"}


def _db_path() -> Path:
    return Path(settings.resolved_database_url.split("///", 1)[1])


def _consistent_copy(source: Path, target: Path) -> None:
    """Copy a live SQLite database safely.

    A plain file copy taken while the poller is mid-write captures pages that
    do not agree with each other. The backup API takes a proper snapshot, and
    the "with" block on a connection commits rather than closes it, so both
    handles are closed by hand.
    """
    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


# ---------------------------------------------------------------------------
# Configuration, on its own
# ---------------------------------------------------------------------------


def export_config(db: Session, include_secrets: bool = False) -> dict:
    """Everything a fresh install would otherwise have to be told again.

    Users are keyed by name rather than id, because the point of this file is
    a different database on a different machine where the ids will not line up.

    Notification channels hold bot tokens and webhook URLs in plain text, so
    they are only included when asked for explicitly, and the caller has to
    have decided that the file is going somewhere safe.
    """
    users = {user.id: user.username for user in db.execute(select(User)).scalars()}

    profiles = []
    for profile in db.execute(select(CostProfile)).scalars():
        owner = users.get(profile.user_id)
        if not owner:
            continue
        data = {
            column.key: getattr(profile, column.key)
            for column in CostProfile.__mapper__.column_attrs
            if column.key not in {"id", "user_id"}
        }
        profiles.append({"user": owner, "profile": data})

    channels = []
    for channel in db.execute(select(NotificationChannel)).scalars():
        owner = users.get(channel.user_id)
        if not owner:
            continue
        entry = {
            "user": owner,
            "type": channel.type.value,
            "name": channel.name,
            "enabled": channel.enabled,
            "is_default": channel.is_default,
            "send_digest": channel.send_digest,
        }
        entry["config"] = dict(channel.config or {}) if include_secrets else None
        channels.append(entry)

    slices = [
        {
            "scope": crawl.scope,
            "provider": crawl.provider,
            "enabled": crawl.enabled,
            "priority": crawl.priority,
            "per_page": crawl.per_page,
            "head_pages": crawl.head_pages,
            "recheck_interval_minutes": crawl.recheck_interval_minutes,
            "full_sweep_interval_days": crawl.full_sweep_interval_days,
        }
        for crawl in db.execute(select(CatalogCrawl)).scalars()
    ]

    app_settings = {
        row.key: row.value for row in db.execute(select(AppSetting)).scalars()
    }
    if not include_secrets:
        # The MFC session cookie is the one runtime setting that is a credential.
        app_settings = {
            key: ("<omitted>" if "cookie" in key or "token" in key or "secret" in key else value)
            for key, value in app_settings.items()
        }

    return {
        "format": ARCHIVE_FORMAT,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "includes_secrets": include_secrets,
        "app_settings": app_settings,
        "cost_profiles": profiles,
        "channels": channels,
        "crawl_slices": slices,
    }


@dataclass
class ConfigImport:
    """What an import actually changed, and what it could not."""

    app_settings: int = 0
    cost_profiles: int = 0
    channels: int = 0
    crawl_slices: int = 0
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "app_settings": self.app_settings,
            "cost_profiles": self.cost_profiles,
            "channels": self.channels,
            "crawl_slices": self.crawl_slices,
            "skipped": self.skipped,
        }


def import_config(db: Session, payload: dict) -> ConfigImport:
    """Apply an exported configuration to this instance.

    Additive and idempotent: it updates what it can match and reports what it
    cannot, rather than deleting anything to make the two sides agree. A user
    named in the file who does not exist here is skipped and named in the
    result, so nothing disappears silently.
    """
    result = ConfigImport()
    if not isinstance(payload, dict):
        raise ValueError("Configuration file is not a JSON object")
    if int(payload.get("format") or 0) > ARCHIVE_FORMAT:
        raise ValueError(
            "This file was written by a newer version of AmiSearch than this one"
        )

    by_name = {
        user.username: user for user in db.execute(select(User)).scalars()
    }

    for key, value in (payload.get("app_settings") or {}).items():
        if value == "<omitted>":
            continue  # exported without secrets; leave whatever is here alone
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value))
        else:
            row.value = value
        result.app_settings += 1

    for entry in payload.get("cost_profiles") or []:
        user = by_name.get(entry.get("user"))
        if user is None:
            result.skipped.append(f"cost profile for unknown user {entry.get('user')!r}")
            continue
        profile = db.execute(
            select(CostProfile).where(CostProfile.user_id == user.id)
        ).scalar_one_or_none()
        if profile is None:
            profile = CostProfile(user_id=user.id)
            db.add(profile)
        known = {
            column.key
            for column in CostProfile.__mapper__.column_attrs
            if column.key not in {"id", "user_id"}
        }
        for key, value in (entry.get("profile") or {}).items():
            if key in known:
                setattr(profile, key, value)
        result.cost_profiles += 1

    for entry in payload.get("channels") or []:
        user = by_name.get(entry.get("user"))
        if user is None:
            result.skipped.append(f"channel for unknown user {entry.get('user')!r}")
            continue
        if not entry.get("config"):
            result.skipped.append(
                f"channel {entry.get('name') or entry.get('type')!r} exported without its"
                " credentials, so it cannot be recreated"
            )
            continue
        try:
            kind = ChannelType(entry["type"])
        except (KeyError, ValueError):
            result.skipped.append(f"channel of unknown type {entry.get('type')!r}")
            continue
        existing = db.execute(
            select(NotificationChannel).where(
                NotificationChannel.user_id == user.id,
                NotificationChannel.type == kind,
                NotificationChannel.name == (entry.get("name") or ""),
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = NotificationChannel(user_id=user.id, type=kind)
            db.add(existing)
        existing.name = entry.get("name") or ""
        existing.config = entry["config"]
        existing.enabled = bool(entry.get("enabled", True))
        existing.is_default = bool(entry.get("is_default", True))
        existing.send_digest = bool(entry.get("send_digest", False))
        result.channels += 1

    for entry in payload.get("crawl_slices") or []:
        crawl = db.execute(
            select(CatalogCrawl).where(
                CatalogCrawl.provider == (entry.get("provider") or "amiami"),
                CatalogCrawl.scope == entry.get("scope"),
            )
        ).scalar_one_or_none()
        if crawl is None:
            result.skipped.append(f"crawl slice {entry.get('scope')!r} does not exist here")
            continue
        for key in (
            "enabled",
            "priority",
            "per_page",
            "head_pages",
            "recheck_interval_minutes",
            "full_sweep_interval_days",
        ):
            if entry.get(key) is not None:
                setattr(crawl, key, entry[key])
        result.crawl_slices += 1

    db.commit()
    return result


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------


def _schema_fingerprint() -> dict:
    """Table and column counts, so a restore can say what it is looking at."""
    inspector = inspect(_engine())
    return {
        name: len(inspector.get_columns(name)) for name in sorted(inspector.get_table_names())
    }


def _engine():
    from ..db import engine

    return engine


def create_archive(
    db: Session,
    destination: Path,
    include_images: bool = False,
    include_secrets: bool = True,
) -> dict:
    """Write a restorable archive to ``destination``.

    The database goes in deflated, which pays off well on a file that is
    mostly text. Photos go in stored, because JPEG data does not compress and
    trying wastes minutes on gigabytes.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = _db_path()
    counts = {"images": 0, "image_bytes": 0}

    with tempfile.TemporaryDirectory(dir=str(destination.parent)) as staging:
        snapshot = Path(staging) / "amisearch.db"
        _consistent_copy(source, snapshot)

        manifest = {
            "format": ARCHIVE_FORMAT,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "includes_images": include_images,
            "includes_secrets": include_secrets,
            "database_bytes": snapshot.stat().st_size,
            "schema": _schema_fingerprint(),
        }
        config = export_config(db, include_secrets=include_secrets)

        # Written to a temporary name so an interrupted download never leaves
        # something that looks like a finished archive.
        partial = destination.with_suffix(destination.suffix + ".partial")
        with zipfile.ZipFile(partial, "w", allowZip64=True) as archive:
            archive.write(snapshot, DB_ENTRY, compress_type=zipfile.ZIP_DEFLATED)
            archive.writestr(
                CONFIG_ENTRY, json.dumps(config, indent=2, default=str),
                compress_type=zipfile.ZIP_DEFLATED,
            )

            if include_images:
                root = Path(settings.data_dir) / "images"
                if root.exists():
                    for path in sorted(root.rglob("*")):
                        if not path.is_file():
                            continue
                        archive.write(
                            path,
                            IMAGE_PREFIX + path.relative_to(root).as_posix(),
                            compress_type=zipfile.ZIP_STORED,
                        )
                        counts["images"] += 1
                        counts["image_bytes"] += path.stat().st_size

            manifest.update(counts)
            archive.writestr(
                MANIFEST_ENTRY, json.dumps(manifest, indent=2),
                compress_type=zipfile.ZIP_DEFLATED,
            )
        partial.replace(destination)

    manifest["archive_bytes"] = destination.stat().st_size
    return manifest


def inspect_archive(path: Path) -> dict:
    """Read an archive's manifest without unpacking or applying anything."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if MANIFEST_ENTRY not in names or DB_ENTRY not in names:
                raise ValueError(
                    "This zip does not look like an AmiSearch backup: it is missing "
                    "its manifest or its database."
                )
            manifest = json.loads(archive.read(MANIFEST_ENTRY))
    except zipfile.BadZipFile as exc:
        raise ValueError("That file is not a readable zip archive") from exc

    if int(manifest.get("format") or 0) > ARCHIVE_FORMAT:
        raise ValueError(
            "This backup was written by a newer version of AmiSearch than this one"
        )
    manifest["image_entries"] = sum(1 for name in names if name.startswith(IMAGE_PREFIX))
    return manifest


def _verify_database(path: Path) -> None:
    """Prove the extracted file is a usable database before anything is swapped."""
    try:
        con = sqlite3.connect(str(path))
    except sqlite3.Error as exc:
        raise ValueError(f"The database in the archive cannot be opened: {exc}") from exc
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError(
                "The database in the archive failed its integrity check, so it "
                "has not been restored."
            )
        tables = {
            row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"The archive does not contain a valid database: {exc}") from exc
    finally:
        con.close()

    missing = REQUIRED_TABLES - tables
    if missing:
        raise ValueError(
            "The database in the archive is missing "
            + ", ".join(sorted(missing))
            + ", so it is not an AmiSearch backup."
        )


@dataclass
class RestoreResult:
    manifest: dict = field(default_factory=dict)
    images_restored: int = 0
    previous_database: str = ""
    migrated_columns: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "manifest": self.manifest,
            "images_restored": self.images_restored,
            "previous_database": self.previous_database,
            "migrated_columns": self.migrated_columns,
        }


def restore_archive(path: Path, restore_images: bool = True) -> RestoreResult:
    """Replace this instance's data with an archive's.

    The order is what keeps this safe. Nothing about the running instance is
    touched until the incoming database has been extracted, opened, integrity
    checked and confirmed to hold the tables it should. Then the outgoing
    database is copied aside under its own name, so a change of mind afterwards
    is still possible, and only then is anything overwritten.

    The overwrite copies *contents* rather than swapping files. Moving the file
    would be the obvious way to do it and is the wrong one: on Windows any
    still-open handle makes the rename fail outright, and on every platform a
    write-ahead log left from the outgoing database can be replayed onto the
    incoming one, which turns a good backup into a corrupt database. Going
    through SQLite's own backup API sidesteps both - it takes the write lock,
    replaces the pages in place, and manages the sidecar files itself.

    Afterwards the restored database is migrated forward, because a backup from
    an older release is exactly the case this has to survive.
    """
    from ..db import create_missing_indexes, engine, migrate_schema
    from ..models import Base

    result = RestoreResult(manifest=inspect_archive(path))
    target = _db_path()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    with tempfile.TemporaryDirectory(dir=str(target.parent)) as staging:
        staged = Path(staging) / "incoming.db"
        with zipfile.ZipFile(path) as archive:
            with archive.open(DB_ENTRY) as src, staged.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)

            _verify_database(staged)

            # Everything below this line changes the running instance.
            if target.exists():
                keep = target.with_name(f"{target.stem}.pre-restore-{stamp}{target.suffix}")
                _consistent_copy(target, keep)
                result.previous_database = keep.name

            # Drop pooled connections so the write lock is uncontended.
            engine.dispose()
            try:
                _consistent_copy(staged, target)
            except sqlite3.Error as exc:
                raise ValueError(
                    "Could not write the restored database"
                    + (
                        f". Your previous data is still in {result.previous_database}: {exc}"
                        if result.previous_database
                        else f": {exc}"
                    )
                ) from exc

            if restore_images:
                root = Path(settings.data_dir) / "images"
                for name in archive.namelist():
                    if not name.startswith(IMAGE_PREFIX) or name.endswith("/"):
                        continue
                    relative = Path(name[len(IMAGE_PREFIX) :])
                    # Never let an archive write outside the image directory.
                    if relative.is_absolute() or ".." in relative.parts:
                        log.warning("Refused suspicious archive entry %s", name)
                        continue
                    out = root / relative
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as src, out.open("wb") as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                    result.images_restored += 1

    # The archive may predate columns this build expects, so bring it forward.
    Base.metadata.create_all(bind=engine)
    result.migrated_columns = migrate_schema()
    create_missing_indexes()
    log.info(
        "Restored from %s: %s image(s), %s column(s) migrated, previous database kept as %s",
        path.name,
        result.images_restored,
        len(result.migrated_columns),
        result.previous_database or "(none)",
    )
    return result
