"""MyFigureCollection client.

MFC has no usable public API any more (api.php and api_v2.php are gone, and
the community REST mirrors are unreliable), so this is a small, polite
scraper over the pages that do work:

  /?keywords=<JAN>&_tb=item          exact item lookup by barcode; MFC
                                     redirects straight to the item page
  /item/<id>                         entries, tags, JAN, picture
  /?_tb=item&tags[]=<slug>&page=<n>  browse items carrying a tag, several
                                     tags combine with AND, rootId=1 limits
                                     the results to figures

Two rules keep this respectful: one shared rate limiter well below what a
person browsing would generate, and aggressive local caching so the same page
is never fetched twice in a session.
"""
from __future__ import annotations

import html as html_lib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

from curl_cffi import requests as curl_requests

from ..providers.ratelimit import CircuitBreaker, TokenBucket

log = logging.getLogger(__name__)

BASE = "https://myfigurecollection.net"
STATIC = "https://static.myfigurecollection.net"

#: MFC is a small community site. Stay far below a normal browsing rate.
REQUESTS_PER_MINUTE = 12
TIMEOUT = 25.0

ROOT_FIGURES = 1
ROOT_GOODS = 2
ROOT_MEDIA = 3

_ITEM_ID_RE = re.compile(r"/item/(\d+)")
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
_TAG_RE = re.compile(
    r'<div class="object-tag">\s*<a class="(?P<auto>[^"]*)" '
    r'href="/\?_tb=item&amp;tags%5B%5D=(?P<slug>[^"]+)" title="(?P<title>[^"]*)"'
    r'>(?P<name>.*?)</a>\s*<a href="/tag/(?P<id>\d+)"',
    re.S,
)
_ENTRY_RE = re.compile(r'href="/entry/(\d+)"[^>]*>(?P<name>.*?)</a>', re.S)
_DATA_FIELD_RE = re.compile(
    r'<div class="data-field">\s*<div class="data-label">(?P<label>.*?)</div>\s*'
    r'<div class="data-value">(?P<value>.*?)</div>',
    re.S,
)
_CARD_RE = re.compile(
    r'href="/item/(?P<id>\d+)" class="anchor item-root-(?P<root>\d+) '
    r'item-category-(?P<category>\d+)[^"]*"><img src="(?P<img>[^"]+)" alt="(?P<alt>[^"]*)"',
    re.S,
)
_PAGES_RE = re.compile(r"Page (\d+) of (\d+)")


def _clean(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    return " ".join(html_lib.unescape(text).split())


@dataclass(slots=True)
class MfcTag:
    slug: str
    name: str
    mfc_id: int | None = None
    is_auto: bool = False


@dataclass(slots=True)
class MfcEntry:
    id: int
    name: str
    kind: str = "tag"


@dataclass(slots=True)
class MfcItem:
    id: int
    url: str
    title: str
    image_url: str | None = None
    jan: str | None = None
    category: str | None = None
    origins: list[MfcEntry] = field(default_factory=list)
    characters: list[MfcEntry] = field(default_factory=list)
    companies: list[MfcEntry] = field(default_factory=list)
    artists: list[MfcEntry] = field(default_factory=list)
    materials: list[MfcEntry] = field(default_factory=list)
    classifications: list[MfcEntry] = field(default_factory=list)
    tags: list[MfcTag] = field(default_factory=list)

    def all_entries(self) -> list[MfcEntry]:
        return [
            *self.origins,
            *self.characters,
            *self.companies,
            *self.artists,
            *self.materials,
            *self.classifications,
        ]


@dataclass(slots=True)
class MfcListing:
    """One card from a browse page. Cheap, no JAN, good enough to seed a search."""

    id: int
    title: str
    image_url: str | None
    root: int
    category: int


class MfcError(RuntimeError):
    pass


class MfcNotFound(MfcError):
    pass


class MfcClient:
    def __init__(self) -> None:
        self.bucket = TokenBucket(rate_per_minute=REQUESTS_PER_MINUTE, burst=4)
        self.breaker = CircuitBreaker(threshold=4, reset_after=300.0)
        # One libcurl session per thread; handles are not thread-safe.
        self._local = threading.local()
        self._sessions: list[curl_requests.Session] = []
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, str]] = {}
        self.cache_ttl = 3600.0
        self.requests_made = 0

    @property
    def session(self) -> curl_requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = curl_requests.Session(
                impersonate="chrome",
                timeout=TIMEOUT,
                allow_redirects=True,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def close(self) -> None:
        with self._lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
        self._local = threading.local()

    def _get(self, path: str) -> tuple[str, str]:
        """Fetch a page. Returns (final_url, html)."""
        url = path if path.startswith("http") else BASE + path
        cached = self._cache.get(url)
        if cached and time.monotonic() - cached[0] < self.cache_ttl:
            return url, cached[1]

        self.breaker.check()
        self.bucket.acquire(timeout=60.0)
        try:
            response = self.session.get(url)
        except Exception as exc:  # noqa: BLE001
            self.breaker.record_failure()
            raise MfcError(f"MyFigureCollection request failed: {exc}") from exc

        self.requests_made += 1
        if response.status_code == 404:
            self.breaker.record_success()
            raise MfcNotFound("MyFigureCollection has no such page")
        if response.status_code >= 400:
            self.breaker.record_failure()
            raise MfcError(f"MyFigureCollection returned {response.status_code}")

        self.breaker.record_success()
        final_url = str(response.url)
        self._cache[url] = (time.monotonic(), response.text)
        return final_url, response.text

    # -- lookup -----------------------------------------------------------
    def find_by_jan(self, jan: str) -> MfcItem | None:
        """Exact lookup by barcode. MFC redirects a matching search to the item."""
        jan = (jan or "").strip()
        if not jan.isdigit() or len(jan) < 8:
            return None
        final_url, html = self._get(f"/?keywords={quote(jan)}&_tb=item")
        match = _ITEM_ID_RE.search(final_url)
        if not match:
            # More than one hit, or none. Only accept an unambiguous redirect.
            return None
        return self._parse_item(int(match.group(1)), final_url, html)

    def get_item(self, mfc_id: int) -> MfcItem:
        final_url, html = self._get(f"/item/{mfc_id}")
        return self._parse_item(mfc_id, final_url, html)

    def search(self, keywords: str, root: int | None = ROOT_FIGURES) -> list[MfcListing]:
        """Keyword search, used when an item has no usable barcode."""
        keywords = (keywords or "").strip()
        if not keywords:
            return []
        path = f"/item/browse/figure/?keywords={quote(keywords)}"
        if root is None:
            path = f"/?_tb=item&keywords={quote(keywords)}"
        final_url, html = self._get(path)

        # A single strong hit redirects straight to the item page.
        direct = _ITEM_ID_RE.search(final_url)
        if direct:
            title = _TITLE_RE.search(html)
            return [
                MfcListing(
                    id=int(direct.group(1)),
                    title=_clean(title.group(1)).replace(" — MyFigureCollection.net", "")
                    if title
                    else "",
                    image_url=None,
                    root=root or 0,
                    category=0,
                )
            ]
        return self._parse_cards(html)

    def browse_tags(
        self,
        tag_slugs: list[str],
        page: int = 1,
        root: int | None = ROOT_FIGURES,
    ) -> tuple[list[MfcListing], int]:
        """Items carrying every one of ``tag_slugs``. Returns (listings, pages)."""
        if not tag_slugs:
            return [], 0
        params = "".join(f"&tags%5B%5D={quote(slug)}" for slug in tag_slugs[:6])
        path = f"/?_tb=item{params}&page={max(1, page)}"
        if root:
            path += f"&rootId={root}"
        _final_url, html = self._get(path)

        pages_match = _PAGES_RE.search(html)
        total_pages = int(pages_match.group(2)) if pages_match else 1
        return self._parse_cards(html), total_pages

    # -- parsing ----------------------------------------------------------
    @staticmethod
    def _parse_cards(html: str) -> list[MfcListing]:
        seen: dict[int, MfcListing] = {}
        for match in _CARD_RE.finditer(html):
            mfc_id = int(match.group("id"))
            if mfc_id in seen:
                continue
            seen[mfc_id] = MfcListing(
                id=mfc_id,
                title=html_lib.unescape(match.group("alt")),
                image_url=match.group("img"),
                root=int(match.group("root")),
                category=int(match.group("category")),
            )
        return list(seen.values())

    @staticmethod
    def _parse_tags(html: str) -> list[MfcTag]:
        tags: list[MfcTag] = []
        seen: set[str] = set()
        for match in _TAG_RE.finditer(html):
            # MFC writes these percent-encoded in the href. Store the decoded
            # form and re-encode when a URL is built, so the slug in our API
            # matches what the site actually calls the tag.
            slug = unquote(html_lib.unescape(match.group("slug")))
            if slug in seen:
                continue
            seen.add(slug)
            tags.append(
                MfcTag(
                    slug=slug,
                    name=html_lib.unescape(match.group("title") or match.group("name")).strip(),
                    mfc_id=int(match.group("id")),
                    is_auto="tag-is-auto" in (match.group("auto") or ""),
                )
            )
        return tags

    #: MFC labels its data rows; these map onto our entry buckets.
    _FIELD_MAP = {
        "origin": "origins",
        "origins": "origins",
        "character": "characters",
        "characters": "characters",
        "company": "companies",
        "companies": "companies",
        "artist": "artists",
        "artists": "artists",
        "material": "materials",
        "materials": "materials",
        "classification": "classifications",
        "classifications": "classifications",
        "category": "classifications",
    }

    def _parse_item(self, mfc_id: int, url: str, html: str) -> MfcItem:
        title_match = _TITLE_RE.search(html)
        title = ""
        if title_match:
            title = _clean(title_match.group(1))
            title = re.sub(r"\s*—\s*MyFigureCollection\.net$", "", title)

        item = MfcItem(id=mfc_id, url=url, title=title)

        for match in _DATA_FIELD_RE.finditer(html):
            label = _clean(match.group("label")).rstrip(":").lower()
            bucket = self._FIELD_MAP.get(label)
            value_html = match.group("value")

            if label in ("barcode", "jan", "jan code", "ean"):
                digits = re.sub(r"\D", "", _clean(value_html))
                if len(digits) >= 8:
                    item.jan = digits
                continue

            if bucket is None:
                continue
            for entry_match in _ENTRY_RE.finditer(value_html):
                name = _clean(entry_match.group("name"))
                if not name:
                    continue
                getattr(item, bucket).append(
                    MfcEntry(id=int(entry_match.group(1)), name=name, kind=bucket[:-1])
                )

        # Some layouts put the barcode outside a data-field row.
        if item.jan is None:
            jan_match = re.search(r"(?:Barcode|JAN)[^0-9]{0,40}(\d{8,14})", html)
            if jan_match:
                item.jan = jan_match.group(1)

        picture = re.search(rf'{re.escape(STATIC)}/upload/items/\d+/{mfc_id}-\w+\.\w+', html)
        if picture:
            item.image_url = picture.group(0)

        item.tags = self._parse_tags(html)

        # The title reads "Origin - Character - Category - ... (Company)". If
        # the labelled rows were missing, fall back to that shape.
        if not item.origins and " - " in title:
            parts = [p.strip() for p in title.split(" - ")]
            if parts:
                item.origins.append(MfcEntry(id=0, name=parts[0], kind="origin"))
            if len(parts) > 1:
                item.characters.append(MfcEntry(id=0, name=parts[1], kind="character"))
        return item

    def status(self) -> dict:
        return {
            "requests_made": self.requests_made,
            "rate_per_minute": self.bucket.rate_per_minute,
            "circuit": self.breaker.snapshot(),
            "cached_pages": len(self._cache),
        }


client = MfcClient()
