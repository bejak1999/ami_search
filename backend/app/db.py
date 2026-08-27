"""Database engine, session factory and schema bootstrap."""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

log = logging.getLogger(__name__)

_connect_args: dict = {}
_engine_kwargs: dict = {"pool_pre_ping": True, "future": True}

if settings.is_sqlite:
    _connect_args["check_same_thread"] = False
else:
    _engine_kwargs["pool_size"] = 10
    _engine_kwargs["max_overflow"] = 20

engine: Engine = create_engine(
    settings.resolved_database_url, connect_args=_connect_args, **_engine_kwargs
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver level
    if not settings.is_sqlite:
        return
    cur = dbapi_connection.cursor()
    # WAL keeps the poller writing while the UI reads, which matters a lot here.
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background jobs."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """Create tables and bring an existing database up to the current schema."""
    from . import models  # noqa: F401  (registers mappers)

    models.Base.metadata.create_all(bind=engine)
    added = migrate_schema()
    if added:
        log.info("Schema migration added %s column(s): %s", len(added), ", ".join(added))
    indexed = create_missing_indexes()
    if indexed:
        log.info("Created %s index(es): %s", len(indexed), ", ".join(indexed))
    adopted = adopt_amiami_rates()
    if adopted:
        log.info("Moved %s cost profile(s) onto AmiAmi's published rate charts", adopted)
    log.info("Database ready at %s", settings.resolved_database_url.split("://", 1)[0])


def adopt_amiami_rates() -> int:
    """Move untouched cost profiles onto AmiAmi's real published rates.

    Before the shop's own rate charts were available, every profile was
    seeded with a made-up weight table. Profiles still carrying that table
    byte for byte were never configured by anyone, so replacing the guess
    with the real thing is a fix rather than an override. A profile whose
    table has been edited at all is left exactly as its owner set it.

    Also fills in the shipping zone from the country, since a fresh column
    otherwise defaults every existing user to Europe.
    """
    from . import models
    from .services import landed_cost, shipping_rates

    seeded = landed_cost.default_profile(0).shipping_table
    moved = 0
    with session_scope() as db:
        try:
            profiles = db.query(models.CostProfile).all()
        except Exception:  # pragma: no cover - table may not exist yet
            return 0
        for profile in profiles:
            if not profile.shipping_zone or profile.shipping_zone == "zone3":
                profile.shipping_zone = shipping_rates.zone_for_country(profile.country)
            if profile.shipping_mode == "table" and profile.shipping_table == seeded:
                profile.shipping_mode = "amiami"
                profile.shipping_service = profile.shipping_service or "auto_air"
                moved += 1
    return moved


def _backup_sqlite() -> Path | None:
    """Copy the SQLite file aside before touching the schema.

    Cheap insurance. An upgrade that goes wrong should never be the reason
    someone loses years of price history.
    """
    if not settings.is_sqlite:
        return None
    source = Path(settings.resolved_database_url.split("///", 1)[1])
    if not source.exists() or source.stat().st_size == 0:
        return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = source.with_name(f"{source.stem}.pre-migration-{stamp}{source.suffix}")
    try:
        # sqlite3's backup API is safe against a live connection, unlike a
        # plain file copy while WAL pages are outstanding.
        with sqlite3.connect(str(source)) as src, sqlite3.connect(str(target)) as dst:
            src.backup(dst)
    except Exception:  # noqa: BLE001 - never let a backup failure block startup
        log.warning("Could not back up the database before migrating", exc_info=True)
        return None

    _prune_backups(source)
    log.info("Backed up the database to %s", target.name)
    return target


def _prune_backups(source: Path, keep: int = 5) -> None:
    backups = sorted(
        source.parent.glob(f"{source.stem}.pre-migration-*{source.suffix}"),
        key=lambda p: p.name,
        reverse=True,
    )
    for stale in backups[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def migrate_schema() -> list[str]:
    """Add any column the models declare but the database is missing.

    Deliberately additive only. Nothing is ever dropped, renamed or retyped,
    so an upgrade cannot destroy data: at worst a column the new code wants is
    reported as unmigratable and the release notes have to explain it.

    The column list is derived from the mappers rather than hand-maintained,
    because a hand-maintained list is exactly the thing someone forgets to
    update when adding a field, and the failure mode is a runtime
    "no such column" on a database that was fine a moment ago.
    """
    from . import models

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    pending: list[tuple[str, str, str]] = []

    for table in models.Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            # create_all just made it, so it is already current.
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            ddl = _column_ddl(column)
            if ddl is None:
                log.warning(
                    "Cannot add %s.%s automatically; it needs a manual migration",
                    table.name,
                    column.name,
                )
                continue
            pending.append((table.name, column.name, ddl))

    if not pending:
        return []

    _backup_sqlite()

    applied: list[str] = []
    for table_name, column_name, ddl in pending:
        statement = f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {ddl}'
        try:
            with engine.begin() as conn:
                conn.execute(text(statement))
            applied.append(f"{table_name}.{column_name}")
        except SQLAlchemyError:
            log.exception("Failed to add %s.%s", table_name, column_name)
    return applied


def _column_ddl(column) -> str | None:
    """Render a column definition, or None when it cannot be added safely."""
    try:
        type_sql = column.type.compile(dialect=engine.dialect)
    except Exception:  # noqa: BLE001 - exotic types are not worth guessing at
        return None

    default = _default_literal(column)

    if not column.nullable:
        # SQLite refuses a NOT NULL column without a default on a populated
        # table, and so does every other engine. Fall back to nullable rather
        # than failing the upgrade; the ORM still enforces it on write.
        if default is None:
            log.info(
                "Adding %s as nullable: NOT NULL needs a default on an existing table",
                column.name,
            )
            return type_sql
        return f"{type_sql} NOT NULL DEFAULT {default}"

    return f"{type_sql} DEFAULT {default}" if default is not None else type_sql


def _default_literal(column) -> str | None:
    """A SQL literal for the column's default, if it has a static one."""
    default = column.default
    if default is None or getattr(default, "is_callable", False):
        # Callable defaults (utcnow, dict, list) are applied by the ORM on
        # insert; existing rows simply get NULL, which is correct.
        return None

    value = getattr(default, "arg", None)
    if value is None or callable(value):
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    return None


def create_missing_indexes() -> list[str]:
    """Create indexes the models declare but the database lacks.

    ``create_all`` only builds indexes alongside a new table, so one added to
    an existing model is silently never created. The symptom is not an error,
    it is a catalogue search that quietly gets slower as rows accumulate,
    which is exactly the kind of thing nobody notices until it is bad.

    Creating an index is additive and safe to repeat.
    """
    from . import models

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    created: list[str] = []

    for table in models.Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        present = {index["name"] for index in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in present:
                continue
            try:
                index.create(bind=engine)
                created.append(index.name)
            except SQLAlchemyError:
                log.exception("Could not create index %s", index.name)
    return created
