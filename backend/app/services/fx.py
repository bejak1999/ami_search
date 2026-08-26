"""Currency conversion.

Two keyless public sources are tried in order, and the result is cached in the
database so a network hiccup never breaks price display. If both are
unreachable we fall back to the last stored rate, however old it is, because a
slightly stale EUR figure beats showing nothing next to a JPY price.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import FxRate, utcnow

log = logging.getLogger(__name__)

SOURCES = (
    ("frankfurter", "https://api.frankfurter.dev/v1/latest"),
    ("exchangerate-api", "https://open.er-api.com/v6/latest/{base}"),
)

QUOTES = ("EUR", "USD", "GBP", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "CAD", "AUD", "JPY")


def _fetch_frankfurter(base: str) -> dict[str, float]:
    quotes = [q for q in QUOTES if q != base]
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        response = client.get(
            SOURCES[0][1], params={"base": base, "symbols": ",".join(quotes)}
        )
    response.raise_for_status()
    return {k: float(v) for k, v in (response.json().get("rates") or {}).items()}


def _fetch_er_api(base: str) -> dict[str, float]:
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        response = client.get(SOURCES[1][1].format(base=base))
    response.raise_for_status()
    payload = response.json()
    if payload.get("result") != "success":
        raise RuntimeError("exchangerate-api reported " + str(payload.get("result")))
    rates = payload.get("rates") or {}
    return {k: float(v) for k, v in rates.items() if k in QUOTES}


def refresh_rates(db: Session, base: str | None = None) -> int:
    """Pull fresh rates for ``base`` and upsert them. Returns rows written."""
    base = (base or settings.fx_base_currency).upper()
    rates: dict[str, float] = {}
    source = ""
    for name, fetcher in (("frankfurter", _fetch_frankfurter), ("exchangerate-api", _fetch_er_api)):
        try:
            rates = fetcher(base)
            source = name
            break
        except Exception as exc:  # noqa: BLE001 - any failure means try the next source
            log.warning("FX source %s failed: %s", name, exc)

    if not rates:
        log.error("All FX sources failed, keeping cached rates")
        return 0

    written = 0
    for quote, rate in rates.items():
        if quote == base:
            continue
        existing = db.execute(
            select(FxRate).where(FxRate.base == base, FxRate.quote == quote)
        ).scalar_one_or_none()
        if existing:
            existing.rate = rate
            existing.source = source
            existing.fetched_at = utcnow()
        else:
            db.add(FxRate(base=base, quote=quote, rate=rate, source=source))
        written += 1
    db.commit()
    log.info("Refreshed %s FX rates for %s from %s", written, base, source)
    return written


def get_rate(db: Session, base: str, quote: str) -> float | None:
    """Conversion factor from ``base`` to ``quote``, or None if unknown."""
    base, quote = base.upper(), quote.upper()
    if base == quote:
        return 1.0

    row = db.execute(
        select(FxRate).where(FxRate.base == base, FxRate.quote == quote)
    ).scalar_one_or_none()
    if row:
        return row.rate

    # Try the inverse pair before giving up.
    inverse = db.execute(
        select(FxRate).where(FxRate.base == quote, FxRate.quote == base)
    ).scalar_one_or_none()
    if inverse and inverse.rate:
        return 1.0 / inverse.rate

    # Last resort: triangulate through the configured base currency.
    pivot = settings.fx_base_currency.upper()
    if pivot not in (base, quote):
        left = get_rate(db, base, pivot)
        right = get_rate(db, pivot, quote)
        if left and right:
            return left * right
    return None


def convert(db: Session, amount: float | None, base: str, quote: str) -> float | None:
    if amount is None:
        return None
    rate = get_rate(db, base, quote)
    return None if rate is None else amount * rate


def rates_age(db: Session, base: str | None = None) -> timedelta | None:
    base = (base or settings.fx_base_currency).upper()
    newest = db.execute(
        select(FxRate.fetched_at)
        .where(FxRate.base == base)
        .order_by(FxRate.fetched_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not newest:
        return None
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - newest


def ensure_fresh(db: Session) -> None:
    """Refresh if the cache is older than the configured interval."""
    age = rates_age(db)
    if age is None or age > timedelta(hours=settings.fx_refresh_hours):
        refresh_rates(db)


def snapshot(db: Session, base: str | None = None) -> dict:
    base = (base or settings.fx_base_currency).upper()
    rows = db.execute(select(FxRate).where(FxRate.base == base)).scalars().all()
    age = rates_age(db, base)
    return {
        "base": base,
        "rates": {r.quote: r.rate for r in rows},
        "source": rows[0].source if rows else None,
        "age_seconds": int(age.total_seconds()) if age else None,
        "stale": bool(age and age > timedelta(hours=settings.fx_refresh_hours * 2)),
    }
