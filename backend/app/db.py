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
    filled = backfill_container_defaults()
    if filled:
        log.info("Filled in %s empty list/dict column(s) left NULL by a migration", filled)
    adopted = adopt_amiami_rates()
    if adopted:
        log.info("Moved %s cost profile(s) onto AmiAmi's published rate charts", adopted)
    grouped = backfill_figure_codes()
    if grouped:
        log.info("Grouped %s item(s) by figure", grouped)
    eased = ease_quiet_slices()
    if eased:
        log.info("Eased the re-read interval on %s slow-moving slice(s)", eased)
    rebuilt = rebuild_price_aggregates()
    if rebuilt:
        log.info("Recomputed the price range on %s item(s) from product-level points", rebuilt)
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


def backfill_figure_codes() -> int:
    """Fill in the figure grouping for rows written before it existed.

    Purely derived from the code already stored, so it is safe to repeat and
    cannot lose anything: rows that already have a value are left alone.
    """
    from sqlalchemy import case, func, literal, select, update

    from . import models

    with session_scope() as db:
        try:
            pending = int(
                db.execute(
                    select(func.count(models.Item.id)).where(
                        models.Item.figure_code.is_(None)
                    )
                ).scalar_one()
                or 0
            )
        except Exception:  # pragma: no cover - table may not exist yet
            return 0
        if not pending:
            return 0
        derived = case(
            (
                models.Item.code.like("%-R"),
                func.substr(models.Item.code, 1, func.length(models.Item.code) - 2),
            ),
            else_=models.Item.code,
        )
        db.execute(
            update(models.Item)
            .where(models.Item.figure_code.is_(None))
            .values(figure_code=derived)
        )
    return pending


def ease_quiet_slices() -> int:
    """Space out the slices that do not need re-reading every half hour.

    Every slice started on the same thirty-minute cadence, which is right for
    used copies - one can be listed and sold inside a morning - and wasteful
    for first-hand stock and pre-orders, which hold the same listings from one
    day to the next. Now that a pass reads the whole slice rather than its
    first few pages, the interval is what controls the cost, so getting it
    right per slice matters more than it used to.

    Only slices still sitting on a value this application shipped are moved.
    An interval someone chose is left exactly as they set it.
    """
    from . import models
    from .services.crawler import DEFAULT_SCOPES

    # Values this application has shipped as a default at some point. A slice
    # still sitting on one of them was never configured by anyone, so moving
    # it is a correction; anything else is a choice its owner made and is left
    # exactly as it is.
    SHIPPED_DEFAULTS = {30, 180, 720, 1440}

    wanted = {spec["scope"]: spec["recheck_interval_minutes"] for spec in DEFAULT_SCOPES}
    changed = 0
    with session_scope() as db:
        try:
            crawls = db.query(models.CatalogCrawl).all()
        except Exception:  # pragma: no cover - table may not exist yet
            return 0
        for crawl in crawls:
            target = wanted.get(crawl.scope)
            current = crawl.recheck_interval_minutes or 30
            if target and current != target and current in SHIPPED_DEFAULTS:
                crawl.recheck_interval_minutes = target
                changed += 1
    return changed


def _container_literal(column) -> str | None:
    """The empty value a JSON column's default would have produced.

    A column declared ``default=list`` has a *callable* default, which the ORM
    runs on insert and which cannot be written into an ALTER TABLE. So a JSON
    column added to an existing table arrives NULL on every row that was
    already there, and stays NULL until something writes to it.

    That is how a dashboard went blank: a list column added by an upgrade read
    back as None, and a response model declaring it a list refused to
    serialise, so the whole endpoint failed rather than one field.
    """
    default = getattr(column, "default", None)
    if default is None or not getattr(default, "is_callable", False):
        return None
    # SQLAlchemy wraps a callable default in one that takes an execution
    # context, so the stored function is never ``list`` itself and comparing
    # against it silently matches nothing. Asking it what it produces is the
    # only reliable way, and it costs one call at start-up.
    try:
        produced = default.arg(None)
    except Exception:  # noqa: BLE001 - a default needing real context is not ours
        return None
    if produced == [] and isinstance(produced, list):
        return "[]"
    if produced == {} and isinstance(produced, dict):
        return "{}"
    return None


def backfill_container_defaults() -> int:
    """Replace NULLs in list and dict columns with their empty value.

    Runs on every start and is idempotent: it only touches rows that are still
    NULL. Cheap, because after the first pass there is nothing to update, and
    it repairs databases migrated by an earlier build as well as the column
    added by this one.
    """
    from . import models

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    filled = 0

    for table in models.Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        present = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            literal = _container_literal(column)
            if literal is None or column.name not in present:
                continue
            statement = text(
                f'UPDATE "{table.name}" SET "{column.name}" = :value '
                f'WHERE "{column.name}" IS NULL'
            )
            try:
                with engine.begin() as conn:
                    result = conn.execute(statement, {"value": literal})
                filled += int(result.rowcount or 0)
            except SQLAlchemyError:
                log.exception("Could not backfill %s.%s", table.name, column.name)
    return filled


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
    # Write under a temporary name and rename on success. A backup interrupted
    # half way would otherwise sit there looking like a real one, and be the
    # file someone reaches for on the worst day of the year.
    staging = target.with_suffix(target.suffix + ".partial")
    try:
        # sqlite3's backup API is safe against a live connection, unlike a
        # plain file copy while WAL pages are outstanding. Note that "with" on
        # a sqlite3 connection commits a transaction, it does not close the
        # handle, so both are closed explicitly - on Windows a leaked handle
        # keeps the file locked.
        src = sqlite3.connect(str(source))
        try:
            dst = sqlite3.connect(str(staging))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        staging.replace(target)
    except Exception:  # noqa: BLE001 - never let a backup failure block startup
        log.warning("Could not back up the database before migrating", exc_info=True)
        staging.unlink(missing_ok=True)
        return None

    _prune_backups(source)
    log.info("Backed up the database to %s", target.name)
    return target


def _prune_backups(source: Path, keep: int = 5) -> None:
    """Keep the most recent few pre-migration copies, discard older ones."""
    backups = sorted(
        (
            path
            for path in source.parent.glob(f"{source.stem}.pre-migration-*{source.suffix}")
            # Never count an interrupted write as one of the copies we keep.
            if path.suffix == source.suffix and path.stat().st_size > 0
        ),
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


def rebuild_price_aggregates() -> int:
    """Recompute lowest/highest/average from the product-level points only.

    A price point with a listing attached is one particular second-hand copy
    at its own grade's price; one without is the product's asking price, the
    cheapest copy on offer. The aggregates on the item row were built over
    both, so an item whose A-grade copy had been sampled reported that grade
    as its highest price ever - true of a copy, false of the product, and the
    figure the "highest seen" box shows.

    Only rows this actually changes are written, so a second run is free and
    the log line stays honest.
    """
    from sqlalchemy import func, select

    from . import models

    changed = 0
    with session_scope() as db:
        try:
            rows = db.execute(
                select(
                    models.PricePoint.item_id,
                    func.min(models.PricePoint.price),
                    func.max(models.PricePoint.price),
                    func.avg(models.PricePoint.price),
                )
                .where(
                    models.PricePoint.listing_id.is_(None),
                    models.PricePoint.price.is_not(None),
                )
                .group_by(models.PricePoint.item_id)
            ).all()
        except Exception:  # pragma: no cover - table may not exist yet
            return 0

        for item_id, low, high, avg in rows:
            item = db.get(models.Item, item_id)
            if item is None:
                continue
            # The current price counts as an observation in its own right: it
            # is what the shop is asking now, whether or not it has been
            # written to the history yet.
            lows = [v for v in (low, item.current_price) if v is not None]
            highs = [v for v in (high, item.current_price) if v is not None]
            wanted_low = min(lows) if lows else None
            wanted_high = max(highs) if highs else None
            wanted_avg = float(avg) if avg is not None else item.current_price
            if (
                item.lowest_price == wanted_low
                and item.highest_price == wanted_high
                and item.average_price == wanted_avg
            ):
                continue
            item.lowest_price = wanted_low
            item.highest_price = wanted_high
            item.average_price = wanted_avg
            changed += 1
        db.commit()
    return changed
