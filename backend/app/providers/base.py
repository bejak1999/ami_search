"""Provider interface.

Adding a shop means implementing :class:`ShopProvider` and registering it.
Nothing outside this package knows what AmiAmi is.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from curl_cffi import requests as curl_requests

from ..config import settings
from .ratelimit import CircuitBreaker, TokenBucket


@dataclass(slots=True)
class NormalizedItem:
    """Shop-agnostic product snapshot."""

    provider: str
    code: str
    name: str
    url: str
    currency: str = "JPY"
    #: The cheapest buyable listing under this product code.
    price: float | None = None
    #: The dearest, when the shop sells several graded copies at once.
    price_max: float | None = None
    list_price: float | None = None
    #: One entry per buyable sub-listing: code, price, condition text.
    variants: list[dict[str, Any]] = field(default_factory=list)
    name_jp: str | None = None
    maker: str | None = None
    series: str | None = None
    character: str | None = None
    category: str | None = None
    scale: str | None = None
    jan_code: str | None = None
    image_url: str | None = None
    images: list[str] = field(default_factory=list)
    condition: str = "new"
    condition_grade: str | None = None
    in_stock: bool = False
    is_preorder: bool = False
    is_backorder: bool = False
    order_closed: bool = False
    sale_status: str | None = None
    release_date: str | None = None
    release_date_parsed: datetime | None = None
    spec: str | None = None
    remarks: str | None = None
    detail_loaded: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchQuery:
    """Normalized search request handed to a provider."""

    keywords: str = ""
    page: int = 1
    per_page: int = 50
    condition: str = "any"          # any | new | preowned
    stock_filter: str = "any"       # any | in_stock | preorder | backorder
    sort: str = "newest"            # newest | price_asc | price_desc | release | relevance
    category_id: int | None = None
    maker_id: int | None = None
    series_id: int | None = None
    character_id: int | None = None
    min_price: float | None = None
    max_price: float | None = None
    exclude_keywords: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchResult:
    items: list[NormalizedItem]
    total: int
    page: int
    per_page: int
    facets: dict[str, Any] = field(default_factory=dict)
    took_ms: int = 0


@dataclass(slots=True)
class FacetOption:
    id: int | str
    name: str
    count: int | None = None


class ProviderError(RuntimeError):
    """Upstream call failed in a way the caller should surface."""


class ItemNotFound(ProviderError):
    pass


class ShopProvider(ABC):
    """Base class holding the shared HTTP client, limiter and breaker."""

    id: str = "base"
    name: str = "Base"
    home_url: str = ""
    currency: str = "JPY"
    supports_facets: bool = False
    #: Short blurb shown in the UI when picking a provider.
    description: str = ""
    #: Browser TLS fingerprint to impersonate. Cloudflare rejects the default
    #: Python ClientHello with a 403 before any header is even inspected, so
    #: this is not optional for AmiAmi.
    impersonate: str = "chrome"

    def __init__(self) -> None:
        self.bucket = TokenBucket(rate_per_minute=settings.provider_requests_per_minute)
        self.breaker = CircuitBreaker()
        self._semaphore = threading.Semaphore(settings.provider_max_concurrency)
        # libcurl handles must not be shared between threads, and this class is
        # used from both the scheduler's worker pool and the request
        # threadpool, so every thread gets its own session.
        self._local = threading.local()
        self._sessions: list[curl_requests.Session] = []
        self._sessions_lock = threading.Lock()
        self.last_latency_ms: float = 0.0

    # -- HTTP plumbing ----------------------------------------------------
    def default_headers(self) -> dict[str, str]:
        return {
            "User-Agent": settings.provider_user_agent,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }

    @property
    def client(self) -> curl_requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = curl_requests.Session(
                headers=self.default_headers(),
                timeout=settings.provider_timeout_seconds,
                impersonate=self.impersonate,
                allow_redirects=True,
            )
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def close(self) -> None:
        with self._sessions_lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
        self._local = threading.local()

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Rate-limited, breaker-guarded HTTP call."""
        import time

        from ..services import reqlog

        self.breaker.check()
        self.bucket.acquire()
        with self._semaphore:
            started = time.monotonic()
            try:
                response = self.client.request(method, url, **kwargs)
            except Exception as exc:  # curl_cffi raises its own error tree
                self.breaker.record_failure()
                reqlog.record(self.id, ok=False)
                raise ProviderError(f"{self.name}: {exc}") from exc
            finally:
                self.last_latency_ms = (time.monotonic() - started) * 1000

        # Counted here rather than at each call site, so nothing that reaches
        # the shop can escape the tally by taking a different route.
        reqlog.record(self.id, ok=response.status_code < 400)

        if response.status_code in (403, 429, 503):
            self.breaker.record_failure()
            raise ProviderError(
                f"{self.name}: upstream returned {response.status_code} "
                "(rate limited or bot-blocked)"
            )
        if response.status_code >= 500:
            self.breaker.record_failure()
            raise ProviderError(f"{self.name}: upstream returned {response.status_code}")

        self.breaker.record_success()
        return response

    # -- Interface --------------------------------------------------------
    @abstractmethod
    def search(self, query: SearchQuery) -> SearchResult:
        """Run a search and return normalized items."""

    @abstractmethod
    def get_item(self, code: str) -> NormalizedItem:
        """Fetch full detail for one product code."""

    def parse_url(self, url: str) -> str | None:
        """Extract a product code from a shop URL, or return None."""
        return None

    def product_url(self, code: str) -> str:
        return self.home_url

    def suggest(self, term: str) -> list[FacetOption]:  # pragma: no cover - optional
        return []

    def health(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "circuit": self.breaker.snapshot(),
            "rate_per_minute": self.bucket.rate_per_minute,
            "last_latency_ms": round(self.last_latency_ms, 1),
        }
