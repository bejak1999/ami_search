"""Backup and restore.

The one operation in this application that can destroy everything in a single
step, so it gets its own suite and its own database. These tests take a real
backup, wreck the instance, put it back, and then spend most of their time on
the paths that matter more than the happy one: what happens when the uploaded
file is rubbish, when it is somebody else's database, or when it comes from an
older release than the code reading it.

Run with:  python tests/test_backup.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# Make "app" importable however this file is invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = tempfile.mkdtemp(prefix="amisearch-backup-")
os.environ["DATA_DIR"] = TMP
os.environ["DATABASE_URL"] = "sqlite:///" + Path(TMP, "amisearch.db").as_posix()
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ.setdefault("SECRET_KEY", "backup-test-secret")

WORK = Path(TMP)
PASS, FAIL = 0, 0


def check(label: str, condition: bool, extra: object = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {extra}")


def _seed() -> None:
    """An instance with something worth losing in it."""
    from app.db import SessionLocal
    from app import models

    with SessionLocal() as db:
        user = models.User(
            username="benni",
            email="b@example.com",
            password_hash="hash",
            role=models.UserRole.admin,
        )
        db.add(user)
        db.commit()
        db.add(
            models.CostProfile(
                user_id=user.id,
                country="DE",
                shipping_mode="amiami",
                shipping_zone="zone3",
                packaging_grams=400,
            )
        )
        db.add(
            models.NotificationChannel(
                user_id=user.id,
                type=models.ChannelType.telegram,
                name="Phone",
                config={"bot_token": "secret-123", "chat_id": "42"},
            )
        )
        item = models.Item(
            provider="amiami",
            code="FIGURE-1-R",
            name="Deleted upstream",
            condition=models.Condition.preowned,
            order_closed=True,
            current_price=8800.0,
            currency="JPY",
        )
        db.add(item)
        db.commit()
        copy = models.Listing(
            item=item,
            provider="amiami",
            code="FIGURE-1-R042",
            sequence=42,
            price=8800.0,
            last_price=8100.0,
            currency="JPY",
            status=models.ListingStatus.gone,
            outcome=models.ListingOutcome.sold,
        )
        db.add(copy)
        db.commit()
        for offset, price in enumerate((8800.0, 8400.0, 8100.0)):
            db.add(
                models.PricePoint(
                    item=item,
                    listing=copy,
                    price=price,
                    currency="JPY",
                    in_stock=True,
                    recorded_at=datetime.now(timezone.utc) - timedelta(days=10 - offset),
                )
            )
        db.commit()


def _snapshot() -> dict:
    from app.db import SessionLocal
    from app import models

    with SessionLocal() as db:
        channel = db.query(models.NotificationChannel).first()
        copy = db.query(models.Listing).first()
        return {
            "users": db.query(models.User).count(),
            "items": db.query(models.Item).count(),
            "listings": db.query(models.Listing).count(),
            "points": db.query(models.PricePoint).count(),
            "channels": db.query(models.NotificationChannel).count(),
            "token": (channel.config or {}).get("bot_token") if channel else None,
            "copy_price": copy.last_price if copy else None,
        }


def test_round_trip() -> None:
    print("\n== A backup put back is the instance you had ==")
    from app.db import SessionLocal
    from app import models
    from app.services import backup, images

    _seed()

    # One cached photo, so the image half is exercised too.
    url = "https://img.amiami.com/images/product/main/x.jpg"
    key = images.key_for(url)
    photo = images.path_for(key)
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b"\xff\xd8\xff" + b"pretend-jpeg" * 500)
    with SessionLocal() as db:
        db.add(
            models.CachedImage(
                key=key, source_url=url, content_type="image/jpeg", bytes=photo.stat().st_size
            )
        )
        db.commit()

    before = _snapshot()
    archive = WORK / "backup.zip"
    with SessionLocal() as db:
        manifest = backup.create_archive(db, archive, include_images=True)

    check("the archive holds a database", manifest["database_bytes"] > 0)
    check("and the photo", manifest["images"] == 1, manifest["images"])
    check("and a schema fingerprint", len(manifest["schema"]) > 10, len(manifest["schema"]))

    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
    check("laid out as expected", {"manifest.json", "config.json"} <= names, sorted(names)[:4])

    # Now lose everything.
    with SessionLocal() as db:
        db.query(models.PricePoint).delete()
        db.query(models.Listing).delete()
        db.query(models.Item).delete()
        db.query(models.NotificationChannel).delete()
        db.commit()
    photo.unlink()
    check("the instance really was wrecked", _snapshot()["items"] == 0)

    result = backup.restore_archive(archive, restore_images=True)
    after = _snapshot()

    check("every row came back", before == after, {"before": before, "after": after})
    check("including the channel's credentials", after["token"] == "secret-123")
    check("the photo is on disk again", photo.exists())
    check("and was counted", result.images_restored == 1, result.images_restored)
    check(
        "the replaced database was kept, not deleted",
        result.previous_database and (WORK / result.previous_database).exists(),
        result.previous_database,
    )


def test_rubbish_is_refused() -> None:
    print("\n== Nothing is touched until the archive is proved good ==")
    from app.db import SessionLocal
    from app import models
    from app.services import backup

    def intact() -> bool:
        with SessionLocal() as db:
            item = (
                db.query(models.Item).filter(models.Item.code == "FIGURE-1-R").one_or_none()
            )
            return item is not None and item.current_price == 8800.0

    check("the instance starts intact", intact())

    plain = WORK / "notazip.zip"
    plain.write_bytes(b"this is just text, not an archive")

    stranger = WORK / "stranger.zip"
    with zipfile.ZipFile(stranger, "w") as z:
        z.writestr("hello.txt", "nothing to see")

    corrupt = WORK / "corrupt.zip"
    with zipfile.ZipFile(corrupt, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": 1}))
        z.writestr("database/amisearch.db", b"SQLite format 3\x00" + b"\x00" * 4000)

    foreign_db = WORK / "foreign.db"
    con = sqlite3.connect(str(foreign_db))
    con.execute("CREATE TABLE recipes (id INTEGER)")
    con.commit()
    con.close()
    foreign = WORK / "foreign.zip"
    with zipfile.ZipFile(foreign, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": 1}))
        z.write(foreign_db, "database/amisearch.db")

    future = WORK / "future.zip"
    with zipfile.ZipFile(future, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": 99}))
        z.writestr("database/amisearch.db", b"x")

    for label, path in (
        ("a plain text file", plain),
        ("somebody else's zip", stranger),
        ("a truncated database", corrupt),
        ("a database that is not ours", foreign),
        ("an archive from a newer release", future),
    ):
        try:
            backup.restore_archive(path)
            refused = False
        except ValueError:
            refused = True
        check(f"{label} is refused", refused)
        check(f"...and the instance survives {label}", intact())


def test_archive_cannot_escape_its_directory() -> None:
    print("\n== An archive cannot write outside the image directory ==")
    from app.services import backup

    source = WORK / "backup.zip"
    hostile = WORK / "hostile.zip"
    with zipfile.ZipFile(hostile, "w") as out, zipfile.ZipFile(source) as src:
        for name in src.namelist():
            out.writestr(name, src.read(name))
        out.writestr("images/../../escaped.txt", "should never be written")

    backup.restore_archive(hostile, restore_images=True)
    check("the traversal entry was not written", not (WORK.parent / "escaped.txt").exists())
    check("nor anywhere inside the data directory", not (WORK / "escaped.txt").exists())


def test_restoring_an_older_backup() -> None:
    print("\n== A backup from an older release is migrated forward ==")
    from app.db import SessionLocal
    from app import models
    from app.services import backup

    source = WORK / "backup.zip"
    staging = WORK / "rewind"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    with zipfile.ZipFile(source) as z:
        z.extractall(staging)

    # Rewind the extracted database to something an older release would have
    # written: no listings table, none of the shelf-life columns.
    old = staging / "database" / "amisearch.db"
    con = sqlite3.connect(str(old))
    con.execute("DROP TABLE listings")
    for (name,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
    ).fetchall():
        if "dwell" in (name or "") or "shelf" in (name or ""):
            con.execute(f"DROP INDEX {name}")
    for column in (
        "dwell_days",
        "dwell_basis",
        "dwell_samples",
        "shelf_due_at",
        "shelf_tier",
        "intake_first_seq",
        "intake_last_seq",
        "listing_count",
        "listing_count_avg",
    ):
        try:
            con.execute(f"ALTER TABLE items DROP COLUMN {column}")
        except sqlite3.OperationalError:
            pass
    con.commit()
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    check("the archive now predates the listings table", "listings" not in tables)

    rewound = WORK / "old-schema.zip"
    with zipfile.ZipFile(rewound, "w") as z:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(staging).as_posix())

    # A write-ahead log left over from the outgoing database. Replaying one of
    # these onto a freshly restored file is how a good backup becomes a broken
    # one, so the restore has to remove it.
    live = WORK / "amisearch.db"
    stale = live.with_name(live.name + "-wal")
    marker = b"stale wal that must never be replayed"
    stale.write_bytes(marker * 100)

    result = backup.restore_archive(rewound, restore_images=False)
    check("columns were added on the way in", len(result.migrated_columns) > 0, result.migrated_columns)
    check(
        "the stale write-ahead log is gone",
        not stale.exists() or marker not in stale.read_bytes(),
    )

    from sqlalchemy import text
    from app.db import SessionLocal as Fresh

    with Fresh() as db:
        check(
            "the restored database passes its integrity check",
            db.execute(text("PRAGMA integrity_check")).scalar() == "ok",
        )
        item = db.query(models.Item).filter(models.Item.code == "FIGURE-1-R").one()
        check("the old rows are readable", item.current_price == 8800.0)
        check("a column added since reads as empty", item.dwell_days is None)
        db.add(
            models.Listing(
                item=item, provider="amiami", code="FIGURE-1-R99", sequence=99,
                price=1.0, currency="JPY",
            )
        )
        db.commit()
        check("and the recreated table is writable", db.query(models.Listing).count() >= 1)


def test_config_travels_on_its_own() -> None:
    print("\n== Settings can move without the database ==")
    from app.db import SessionLocal
    from app import models
    from app.services import backup

    with SessionLocal() as db:
        db.add(models.AppSetting(key="mfc_session_cookie", value={"v": "abc123"}))
        db.add(models.AppSetting(key="ui_note", value={"v": "hello"}))
        db.commit()

        safe = backup.export_config(db, include_secrets=False)
        full = backup.export_config(db, include_secrets=True)

    check(
        "a safe export withholds the cookie",
        safe["app_settings"]["mfc_session_cookie"] == "<omitted>",
        safe["app_settings"],
    )
    check("but keeps harmless settings", safe["app_settings"]["ui_note"] == {"v": "hello"})
    check(
        "and strips channel credentials",
        all(entry["config"] is None for entry in safe["channels"]),
    )
    check(
        "a full export carries them",
        any((entry["config"] or {}).get("bot_token") == "secret-123" for entry in full["channels"]),
    )
    check("cost profiles travel", full["cost_profiles"][0]["profile"]["shipping_zone"] == "zone3")

    # Change everything, then import the file back over it.
    with SessionLocal() as db:
        profile = db.query(models.CostProfile).one()
        profile.shipping_zone = "zone5"
        profile.packaging_grams = 999
        channel = db.query(models.NotificationChannel).first()
        channel.config = {"bot_token": "wrong"}
        db.query(models.AppSetting).filter(models.AppSetting.key == "ui_note").delete()
        db.commit()

        result = backup.import_config(db, full)
        db.expire_all()
        profile = db.query(models.CostProfile).one()
        channel = db.query(models.NotificationChannel).first()

    check("the profile was put back", profile.shipping_zone == "zone3", profile.shipping_zone)
    check("including its packaging weight", profile.packaging_grams == 400)
    check("the channel's token was put back", channel.config.get("bot_token") == "secret-123")
    check("a deleted setting reappeared", result.app_settings >= 2, result.as_dict())

    # A safe export cannot recreate a channel, and must say so rather than
    # writing a channel with no credentials in it.
    with SessionLocal() as db:
        before = db.query(models.NotificationChannel).count()
        outcome = backup.import_config(db, safe)
        after = db.query(models.NotificationChannel).count()
    check("importing a redacted export adds no broken channel", before == after)
    check(
        "and explains why it skipped it",
        any("credentials" in note for note in outcome.skipped),
        outcome.skipped,
    )

    with SessionLocal() as db:
        rejected = False
        try:
            backup.import_config(db, {"format": 999})
        except ValueError:
            rejected = True
    check("a config file from the future is refused", rejected)


def main() -> int:
    from app.db import init_db

    init_db()
    test_round_trip()
    test_rubbish_is_refused()
    test_archive_cannot_escape_its_directory()
    test_restoring_an_older_backup()
    test_config_travels_on_its_own()

    print("\n" + "=" * 46)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 46)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
