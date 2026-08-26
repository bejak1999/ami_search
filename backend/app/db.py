"""Database engine, session factory and schema bootstrap."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
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
    """Create tables and apply the small set of additive migrations we need."""
    from . import models  # noqa: F401  (registers mappers)

    models.Base.metadata.create_all(bind=engine)
    _run_light_migrations()
    log.info("Database ready at %s", settings.resolved_database_url.split("://", 1)[0])


# Columns added after the first public release. Each entry is
# (table, column, DDL type + default) and is applied only when missing.
_ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("watches", "baselined", "BOOLEAN DEFAULT 0 NOT NULL"),
    ("watch_seen_items", "last_compare_price", "FLOAT"),
    ("items", "mfc_id", "INTEGER"),
    ("items", "mfc_matched_by", "VARCHAR(16)"),
    ("items", "mfc_url", "TEXT"),
    ("items", "mfc_confidence", "FLOAT"),
    ("items", "mfc_fetched_at", "TIMESTAMP"),
    ("items", "mfc_attempts", "INTEGER DEFAULT 0 NOT NULL"),
]


def _run_light_migrations() -> None:
    """Additive-only migrations, safe to run on every boot."""
    if not _ADDITIVE_COLUMNS:
        return
    from sqlalchemy import inspect

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in _ADDITIVE_COLUMNS:
            if table not in existing_tables:
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            if column in cols:
                continue
            log.info("Adding column %s.%s", table, column)
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
