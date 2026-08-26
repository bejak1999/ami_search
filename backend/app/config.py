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

    # ---- Retention --------------------------------------------------------
    price_history_retention_days: int = 1095
    alert_retention_days: int = 365

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
