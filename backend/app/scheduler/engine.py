"""The polling engine.

A single ticker wakes up every few seconds, asks the database which watches
are due, and hands them to a small thread pool. Watches are not registered as
individual APScheduler jobs on purpose: intervals change adaptively after
every run, and a due-query is far simpler to reason about than several hundred
jobs being rescheduled constantly.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import or_, select

from ..config import settings
from ..db import session_scope
from ..models import Watch
from ..services import (
    catalog,
    crawler,
    dealradar,
    digest,
    enrich,
    fx,
    health,
    images,
    matcher,
    shelfwatch,
)

log = logging.getLogger(__name__)

TICK_SECONDS = 5
MAX_BATCH = 25


class PollingEngine:
    def __init__(self) -> None:
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self.pool = ThreadPoolExecutor(
            max_workers=settings.worker_concurrency, thread_name_prefix="poll"
        )
        self._inflight: set[int] = set()
        self._lock = threading.Lock()
        self._started = False
        self.last_tick: datetime | None = None
        self.runs_total = 0
        self.alerts_total = 0
        self.errors_total = 0
        self.crawl_pages_total = 0
        self.crawl_items_total = 0
        self.last_crawl: dict | None = None
        self.last_enrichment: dict | None = None
        self.last_health: dict | None = None
        self.last_image_prefetch: dict | None = None
        self.shelf_checked_total = 0
        self.shelf_vanished_total = 0
        self.last_shelfwatch: dict | None = None
        #: Set on shutdown so long-running work can bail at a safe boundary.
        self.stopping = False

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._started or not settings.scheduler_enabled:
            log.info("Scheduler disabled by configuration")
            return
        self.scheduler.add_job(
            self.tick,
            "interval",
            seconds=TICK_SECONDS,
            id="tick",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.refresh_fx,
            "interval",
            hours=max(1, settings.fx_refresh_hours),
            id="fx",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
        )
        self.scheduler.add_job(
            self.run_deal_radar, "interval", hours=6, id="deal_radar", max_instances=1
        )
        # Build the local catalogue in the background. Discovery is only as
        # good as the corpus behind it, and a corpus made of whatever the user
        # happened to search for is not one.
        if settings.crawler_enabled:
            self.scheduler.add_job(
                self.run_crawler,
                "interval",
                minutes=max(1, settings.crawler_run_interval_minutes),
                id="catalog_crawl",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
            )

        # Keep product photos locally. A sold pre-owned listing takes its
        # images with it, and by then this instance holds the only record.
        if settings.image_cache_enabled:
            self.scheduler.add_job(
                self.run_image_prefetch,
                "interval",
                minutes=max(1, settings.image_prefetch_interval_minutes),
                id="image_prefetch",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
            )

        # Follow individual pre-owned copies so a vanished listing becomes a
        # recorded sale rather than a hole where a listing used to be.
        if settings.shelf_tracking_enabled:
            self.scheduler.add_job(
                self.run_shelfwatch,
                "interval",
                minutes=max(1, settings.shelf_run_interval_minutes),
                id="shelfwatch",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=4),
            )

        # MyFigureCollection is scraped slowly and steadily in the background,
        # so the tag index fills in without ever bursting on that site.
        if settings.mfc_enabled:
            self.scheduler.add_job(
                self.run_enrichment,
                "interval",
                minutes=max(1, settings.mfc_run_interval_minutes),
                id="mfc_enrich",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        self.scheduler.add_job(
            self.housekeeping, "cron", hour=4, minute=17, id="housekeeping", max_instances=1
        )
        # Watch the machinery itself. A tracker that silently stops working is
        # worse than one that never worked, because you carry on trusting it.
        self.scheduler.add_job(
            self.run_health_check,
            "interval",
            minutes=max(5, settings.health_check_interval_minutes),
            id="health",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3),
        )
        self.scheduler.add_job(
            self.run_digests, "cron", minute=0, id="digests", max_instances=1
        )
        self.scheduler.start()
        self._started = True
        log.info(
            "Polling engine started: %s workers, %ss tick, %s req/min upstream budget",
            settings.worker_concurrency,
            TICK_SECONDS,
            settings.provider_requests_per_minute,
        )

    def shutdown(self, timeout: float = 20.0) -> None:
        """Stop accepting work, then give what is running a chance to finish.

        A container update sends SIGTERM mid-flight. Python cannot interrupt a
        running thread, so tearing the pool down without waiting would let a
        half-finished poll keep going while the interpreter shuts down around
        it. Everything commits incrementally, so nothing is corrupted either
        way, but waiting means a watch that already fetched its results still
        gets to record them instead of repeating the request after the
        restart.
        """
        self.stopping = True
        if self._started:
            # Stop firing new jobs first; existing ones are left to finish.
            self.scheduler.shutdown(wait=False)
            self._started = False

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if not self._inflight:
                    break
            time.sleep(0.2)

        with self._lock:
            stragglers = len(self._inflight)
        if stragglers:
            log.warning("Shutting down with %s check(s) still running", stragglers)

        self.pool.shutdown(wait=False, cancel_futures=True)
        log.info("Polling engine stopped")

    # -- ticking ----------------------------------------------------------
    def due_watch_ids(self, limit: int = MAX_BATCH) -> list[int]:
        now = datetime.now(timezone.utc)
        with session_scope() as db:
            stmt = (
                select(Watch.id)
                .where(
                    Watch.enabled.is_(True),
                    or_(Watch.next_run_at.is_(None), Watch.next_run_at <= now),
                )
                .order_by(Watch.priority.desc(), Watch.next_run_at.asc().nulls_first())
                .limit(limit)
            )
            return list(db.execute(stmt).scalars().all())

    def tick(self) -> None:
        self.last_tick = datetime.now(timezone.utc)
        try:
            candidates = self.due_watch_ids()
        except Exception:  # noqa: BLE001 - a failing tick must not kill the scheduler
            log.exception("Failed to query due watches")
            return

        for watch_id in candidates:
            with self._lock:
                if watch_id in self._inflight:
                    continue
                if len(self._inflight) >= settings.worker_concurrency * 4:
                    break
                self._inflight.add(watch_id)
            future = self.pool.submit(self._run_one, watch_id)
            future.add_done_callback(lambda _f, wid=watch_id: self._release(wid))

    def _release(self, watch_id: int) -> None:
        with self._lock:
            self._inflight.discard(watch_id)

    def _run_one(self, watch_id: int) -> None:
        try:
            with session_scope() as db:
                watch = db.get(Watch, watch_id)
                if watch is None or not watch.enabled:
                    return
                outcome = matcher.run_watch(db, watch)
            self.runs_total += 1
            self.alerts_total += outcome.alerts
            if outcome.error:
                self.errors_total += 1
        except Exception:  # noqa: BLE001
            self.errors_total += 1
            log.exception("Watch %s crashed", watch_id)
            # Push it out so a poison-pill watch cannot spin the whole pool.
            try:
                with session_scope() as db:
                    watch = db.get(Watch, watch_id)
                    if watch is not None:
                        watch.consecutive_errors += 1
                        watch.last_error = "Internal error, see server logs"
                        watch.next_run_at = datetime.now(timezone.utc) + timedelta(minutes=10)
            except Exception:  # noqa: BLE001
                log.exception("Could not defer failing watch %s", watch_id)

    def run_now(self, watch_id: int) -> Future:
        """Manual 'check now' from the UI."""
        with self._lock:
            self._inflight.add(watch_id)
        future = self.pool.submit(self._run_one, watch_id)
        future.add_done_callback(lambda _f, wid=watch_id: self._release(wid))
        return future

    # -- periodic maintenance ---------------------------------------------
    def refresh_fx(self) -> None:
        try:
            with session_scope() as db:
                fx.refresh_rates(db)
        except Exception:  # noqa: BLE001
            log.exception("FX refresh failed")

    def run_deal_radar(self) -> None:
        try:
            with session_scope() as db:
                found = dealradar.scan(db)
            if found:
                log.info("Deal radar raised %s alerts", found)
        except Exception:  # noqa: BLE001
            log.exception("Deal radar failed")

    def run_crawler(self) -> None:
        try:
            with session_scope() as db:
                outcome = crawler.run_once(db)
            if outcome.pages:
                self.crawl_pages_total += outcome.pages
                self.crawl_items_total += outcome.items
            self.last_crawl = outcome.as_dict()
        except Exception:  # noqa: BLE001
            log.exception("Catalogue crawl failed")

    def run_shelfwatch(self) -> None:
        try:
            with session_scope() as db:
                outcome = shelfwatch.run_once(db)
            if outcome.checked:
                self.shelf_checked_total += outcome.checked
                self.shelf_vanished_total += outcome.vanished
            self.last_shelfwatch = outcome.as_dict()
        except Exception:  # noqa: BLE001
            log.exception("Shelf-life sampling failed")

    def run_enrichment(self) -> None:
        try:
            with session_scope() as db:
                outcome = enrich.run_batch(db, limit=settings.mfc_batch_size)
            self.last_enrichment = outcome
        except Exception:  # noqa: BLE001
            log.exception("MFC enrichment batch failed")

    def run_image_prefetch(self) -> None:
        try:
            with session_scope() as db:
                self.last_image_prefetch = images.prefetch(db)
                images.prune(db)
        except Exception:  # noqa: BLE001
            log.exception("Image prefetch failed")

    def run_health_check(self) -> None:
        try:
            with session_scope() as db:
                self.last_health = health.check(db, notify_enabled=settings.health_alerts_enabled)
        except Exception:  # noqa: BLE001
            log.exception("Health check failed")

    def run_digests(self) -> None:
        try:
            with session_scope() as db:
                digest.dispatch_due(db)
        except Exception:  # noqa: BLE001
            log.exception("Digest dispatch failed")

    def housekeeping(self) -> None:
        try:
            with session_scope() as db:
                catalog.prune_history(db, settings.price_history_retention_days)
                digest.prune_alerts(db, settings.alert_retention_days)
        except Exception:  # noqa: BLE001
            log.exception("Housekeeping failed")

    @contextmanager
    def paused(self):
        """Hold every background job still for the duration of the block.

        Used while the database file is being swapped out from under the
        application. Jobs already running are asked to stop at their next safe
        boundary through the same flag a container shutdown uses, and the
        scheduler is told not to start any more.
        """
        was_running = self._started and self.scheduler.running
        previous = self.stopping
        self.stopping = True
        if was_running:
            try:
                self.scheduler.pause()
            except Exception:  # noqa: BLE001 - pausing must never be fatal
                log.warning("Could not pause the scheduler", exc_info=True)
                was_running = False
        try:
            yield
        finally:
            self.stopping = previous
            if was_running:
                try:
                    self.scheduler.resume()
                except Exception:  # noqa: BLE001
                    log.exception("Could not resume the scheduler after a pause")

    # -- introspection -----------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            inflight = len(self._inflight)
        return {
            "running": self._started,
            "last_tick": self.last_tick,
            "tick_seconds": TICK_SECONDS,
            "workers": settings.worker_concurrency,
            "inflight": inflight,
            "runs_total": self.runs_total,
            "alerts_total": self.alerts_total,
            "errors_total": self.errors_total,
            "adaptive": settings.adaptive_polling,
            "min_interval_seconds": settings.min_poll_interval_seconds,
            "crawler_enabled": settings.crawler_enabled,
            "crawl_pages_total": self.crawl_pages_total,
            "crawl_items_total": self.crawl_items_total,
            "last_crawl": self.last_crawl,
            "last_enrichment": self.last_enrichment,
            "last_health": self.last_health,
            "last_image_prefetch": self.last_image_prefetch,
        }


engine = PollingEngine()
