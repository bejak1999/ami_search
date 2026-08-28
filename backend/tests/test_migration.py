"""Upgrade safety.

The promise is that pulling a new image never costs you data. This builds a
database shaped like an older release, boots the current schema against it,
and asserts that every row survived and that the new columns arrived.

Run with:  python tests/test_migration.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# Make "app" importable however this file is invoked, so the suites run with a
# plain "python tests/<name>.py" and do not depend on PYTHONPATH being set.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = tempfile.mkdtemp(prefix="amisearch-migration-")
os.environ["DATA_DIR"] = TMP
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ.setdefault("SECRET_KEY", "migration-test-secret")

DB_PATH = os.path.join(TMP, "amisearch.db")

PASS, FAIL = 0, 0


def check(label: str, condition: bool, extra: object = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {extra}")


# A deliberately reduced version of the schema: enough columns for the ORM to
# have been happy at the time, missing everything added since.
LEGACY_SCHEMA = """
CREATE TABLE items (
    id INTEGER PRIMARY KEY,
    provider VARCHAR(32) NOT NULL DEFAULT 'amiami',
    code VARCHAR(64) NOT NULL,
    name TEXT NOT NULL,
    currency VARCHAR(3) NOT NULL DEFAULT 'JPY',
    current_price FLOAT,
    list_price FLOAT,
    condition VARCHAR(8) NOT NULL DEFAULT 'new',
    in_stock BOOLEAN NOT NULL DEFAULT 0,
    is_preorder BOOLEAN NOT NULL DEFAULT 0,
    is_backorder BOOLEAN NOT NULL DEFAULT 0,
    order_closed BOOLEAN NOT NULL DEFAULT 0,
    detail_loaded BOOLEAN NOT NULL DEFAULT 0,
    mfc_attempts INTEGER NOT NULL DEFAULT 0,
    images TEXT NOT NULL DEFAULT '[]',
    raw TEXT NOT NULL DEFAULT '{}',
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX uq_item_provider_code ON items (provider, code);

CREATE TABLE price_points (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    price FLOAT,
    currency VARCHAR(3) NOT NULL DEFAULT 'JPY',
    in_stock BOOLEAN NOT NULL DEFAULT 0
);
"""

LEGACY_ROWS = """
INSERT INTO items (code, name, current_price, list_price, condition) VALUES
    ('FIGURE-165063-R', 'Rias Gremory Bunny 1/4', 40180, 38500, 'preowned'),
    ('FIGURE-153570-R', 'Symphogear Tsubasa 1/7', 10380, 29700, 'preowned');
INSERT INTO price_points (item_id, price) VALUES
    (1, 45000), (1, 42000), (1, 40180), (2, 12000), (2, 10380);
"""


def snapshot() -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        return {
            "items": conn.execute("SELECT code, current_price FROM items ORDER BY id").fetchall(),
            "points": conn.execute("SELECT COUNT(*) FROM price_points").fetchone()[0],
            "item_columns": {row[1] for row in conn.execute("PRAGMA table_info(items)")},
            "tables": {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            },
        }
    finally:
        conn.close()


def main() -> int:
    print()
    print("== Upgrading a database written by an older release ==")

    legacy = sqlite3.connect(DB_PATH)
    legacy.executescript(LEGACY_SCHEMA)
    legacy.executescript(LEGACY_ROWS)
    legacy.commit()
    legacy.close()

    before = snapshot()
    check("legacy database has rows", len(before["items"]) == 2, before["items"])
    check("it predates price_max", "price_max" not in before["item_columns"])
    check("it predates variants", "variants" not in before["item_columns"])

    from app.db import SessionLocal, init_db

    init_db()
    after = snapshot()

    check("every item survived", after["items"] == before["items"], after["items"])
    check("every price point survived", after["points"] == before["points"], after["points"])
    check("nothing was dropped", before["item_columns"] <= after["item_columns"])
    check("price_max was added", "price_max" in after["item_columns"])
    check("variants was added", "variants" in after["item_columns"])
    check("new tables were created", len(after["tables"]) > len(before["tables"]))

    backups = [name for name in os.listdir(TMP) if "pre-migration" in name]
    check("a backup was taken first", len(backups) == 1, backups)

    # The ORM has to be able to read the migrated rows through the new model.
    from app.models import Item

    db = SessionLocal()
    try:
        row = db.query(Item).filter_by(code="FIGURE-165063-R").one()
        check("the ORM reads a migrated row", row.current_price == 40180)
        check("a column added today reads as empty, not as an error", row.price_max is None)
        # "or []" was hiding the very thing this needed to catch: the column
        # really was NULL, every reader had to remember to paper over it, and
        # the one that did not - a response model declaring a list - took a
        # whole endpoint down with it.
        check("a JSON column is filled in, not left NULL", row.variants == [])

        # The same sweep the application runs, asserted over every table: a
        # list or dict column must never survive a migration as NULL.
        from sqlalchemy import text as _text

        from app.db import _container_literal
        from app.models import Base

        offenders = []
        for table in Base.metadata.sorted_tables:
            for column in table.columns:
                if _container_literal(column) is None:
                    continue
                nulls = db.execute(
                    _text(
                        f'SELECT COUNT(*) FROM "{table.name}" '
                        f'WHERE "{column.name}" IS NULL'
                    )
                ).scalar_one()
                if nulls:
                    offenders.append(f"{table.name}.{column.name}={nulls}")
        check("no list or dict column is left NULL anywhere", not offenders, offenders)

        # And the response models must survive a migrated row, because a
        # single unset field should never be able to empty a page.
        from app.api.serializers import item_out

        payload = item_out(db, row)
        check("a migrated row still serialises", payload.code == "FIGURE-165063-R")
    finally:
        db.close()

    # Running it again must be a no-op rather than a second round of ALTERs.
    init_db()
    again = snapshot()
    check("re-running the migration changes nothing", again == after)
    check(
        "and does not pile up backups",
        len([n for n in os.listdir(TMP) if "pre-migration" in n]) == 1,
    )

    print()
    print("=" * 46)
    print(f"  {PASS} passed, {FAIL} failed")
    print("=" * 46)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
