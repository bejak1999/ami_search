"""Application configuration, loaded from environment variables."""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- Core -------------------------------------------------------------
    app_name: str = "AmiSearch"
    data_dir: Path = Path("/data")
    secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    log_level: str = "INFO"
    base_url: str = "http://localhost:8080"
    """Public URL of this instance. Used in notification deep links."""

    # ---- Database ---------------------------------------------------------
    database_url: str = ""
    """Leave empty to use SQLite inside DATA_DIR. Supports postgresql+psycopg://..."""

    # ---- Auth -------------------------------------------------------------
    access_token_ttl_minutes: int = 60 * 24 * 14
    allow_registration: bool = True
    """First registered account always becomes an administrator."""

    # ---- Scheduler / polling ---------------------------------------------
    scheduler_enabled: bool = True
    worker_concurrency: int = 4
    min_poll_interval_seconds: int = 15
    """Hard floor. No watch may poll faster than this, whatever the user sets."""
    default_poll_interval_seconds: int = 300
    adaptive_polling: bool = True

    # ---- Upstream rate limiting ------------------------------------------
    provider_requests_per_minute: int = 40
    provider_max_concurrency: int = 3
    provider_timeout_seconds: float = 20.0
    provider_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    amiami_api_key: str = "amiami_dev"

    # ---- Catalogue ingest --------------------------------------------------
    crawler_enabled: bool = True
    """Walk the shop catalogue in the background so discovery has a corpus."""
    crawler_requests_per_minute: float = 8.0
    """The crawler's own budget. It also passes through the shared provider
    limiter, and it yields entirely whenever a watch is due, so alerts always
    win the race for the request budget."""
    crawler_max_seconds_per_run: int = 240
    crawler_run_interval_minutes: int = 5
    crawler_jitter_sigma: float = 0.55
    crawler_break_probability: float = 0.07
    crawler_quiet_hours_start: int = 1
    crawler_quiet_hours_end: int = 7
    crawler_quiet_slowdown: float = 2.5

    # ---- MyFigureCollection ------------------------------------------------
    mfc_enabled: bool = True
    mfc_requests_per_minute: float = 10.0

    # -- Shelf-life sampling ---------------------------------------------
    #: Follow individual pre-owned copies so "how long was it listed" has an
    #: answer. Off means the intake-counter estimate still works, since that
    #: rides along on detail fetches that happen for other reasons anyway.
    shelf_tracking_enabled: bool = True
    shelf_run_interval_minutes: int = 10
    shelf_max_seconds_per_run: int = 240
    shelf_requests_per_minute: float = 10.0
    #: How often each tier gets looked at. Hot is the handful of products
    #: someone is actually waiting on, where the tight bound is worth paying
    #: for; cold is the long tail, which only has to feed the statistics.
    shelf_hot_interval_hours: float = 2.0
    shelf_warm_interval_hours: float = 12.0
    shelf_cold_interval_hours: float = 72.0
    #: 0 means "work it out from the rate limit", which is almost always what
    #: you want: a fixed batch silently caps throughput far below the budget
    #: you configured. Eight items every five minutes allowed 96 an hour while
    #: MFC_REQUESTS_PER_MINUTE was permitting roughly three times that, and the
    #: backlog looked stuck for no visible reason. Set a number to override.
    mfc_batch_size: int = 0
    mfc_run_interval_minutes: int = 5
    mfc_session_cookie: str = ""
    """Optional PHPSESSID from a signed-in MyFigureCollection browser session.

    Without it MFC serves a bare 404 for entries it restricts to signed-in
    members, chiefly adult ones, so those items can be identified by barcode
    but never tagged. Supplying a session makes them readable.

    A cookie is taken rather than a username and password on purpose: it never
    puts the account password in this database, and signing out on
    MyFigureCollection revokes it immediately.
    """

    # ---- FX ---------------------------------------------------------------
    fx_refresh_hours: int = 6
    fx_base_currency: str = "JPY"
    display_currency: str = "EUR"

    # ---- Notifications ----------------------------------------------------
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:admin@example.com"

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False

    # ---- Image cache --------------------------------------------------------
    image_cache_enabled: bool = True
    """Keep product photos on disk.

    AmiAmi deletes a pre-owned listing as soon as it sells and its images go
    with it, so without a local copy the record of a figure that sold shows a
    broken frame at exactly the point the history becomes interesting.
    """
    image_cache_max_gb: float = 25.0
    """Soft budget. The least recently shown images are dropped past this.

    Only the replaceable ones, though: pre-owned photographs are kept even
    when that leaves the cache over budget, because nothing can fetch them
    back. So this is really a budget for product shots, and it needs enough
    headroom that eviction has something to work with once the used copies
    have taken their share.
    """
    image_cache_full_images: bool = True
    """Cache the large detail photo as well as the grid thumbnail. Thumbnails
    are about 4 KB each; full images are around 80 KB."""
    image_cache_requests_per_minute: float = 60.0
    #: Per run. With a five-minute interval this averages fifty a minute,
    #: inside the allowance above, which the token bucket enforces anyway.
    #: It used to be forty per run - eight a minute - which would have
    #: taken eleven days to work through the backlog even once the
    #: prefetcher could see it.
    image_prefetch_batch: int = 250
    image_prefetch_interval_minutes: int = 5

    # ---- Self-monitoring ---------------------------------------------------
    health_alerts_enabled: bool = True
    """Tell administrators, on their own notification channels, when the
    scraping machinery breaks: a shop refusing requests, an expired
    MyFigureCollection session, a notification channel that stopped
    accepting messages."""
    health_check_interval_minutes: int = 15

    # ---- Retention --------------------------------------------------------
    price_history_retention_days: int = 1095
    alert_retention_days: int = 365

    @property
    def mfc_effective_batch_size(self) -> int:
        """How many items one enrichment pass should attempt.

        Linking an item costs about two requests: a barcode lookup, then the
        entry page it resolves to. Sizing the batch from the rate limit and the
        interval keeps the two settings in step, so raising the request budget
        actually speeds the backlog up instead of doing nothing at all.
        """
        if self.mfc_batch_size > 0:
            return self.mfc_batch_size
        per_run = self.mfc_requests_per_minute * max(1, self.mfc_run_interval_minutes)
        return max(1, int(per_run / 2.0))

    @field_validator("data_dir", mode="before")
    @classmethod
    def _expand(cls, v: object) -> object:
        return Path(str(v)).expanduser() if v else v

    @property
    def resolved_database_url(self) -> str:
        """SQLAlchemy URL. Defaults to a SQLite file inside DATA_DIR."""
        if self.database_url:
            # Accept the plain postgres:// form that many hosts hand out.
            if self.database_url.startswith("postgres://"):
                return self.database_url.replace("postgres://", "postgresql+psycopg://", 1)
            if self.database_url.startswith("postgresql://"):
                return self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'amisearch.db').as_posix()}"

    @property
    def is_sqlite(self) -> bool:
        return self.resolved_database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()
