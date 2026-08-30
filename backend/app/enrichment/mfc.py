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

from ..config import settings
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
    #: True when the entry exists and the id is certain, but the page itself
    #: is withheld from signed-out visitors, so no tags could be read.
    restricted: bool = False
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
        #: Overrides the configured session at runtime, so the admin view can
        #: change it without a restart.
        self._session_cookie: str | None = None

    @property
    def session_cookie(self) -> str:
        if self._session_cookie is not None:
            return self._session_cookie
        return settings.mfc_session_cookie or ""

    @staticmethod
    def parse_cookies(value: str | None) -> dict[str, str]:
        """Accept whatever the user managed to copy.

        Three shapes all work, because asking someone to isolate exactly the
        right line out of a cookie manager is a good way to have them paste
        the consent string instead:

          "abc123"                          a bare PHPSESSID value
          "PHPSESSID=abc123"                one named pair
          "PHPSESSID=abc123; other=x"       a whole cookie header

        Everything named is kept, since a signed-in session may rest on more
        than one cookie, and irrelevant extras do no harm.
        """
        raw = (value or "").strip().strip(";")
        if not raw:
            return {}
        if "=" not in raw:
            # A bare value is by far the most likely thing to be pasted.
            return {"PHPSESSID": raw}

        cookies: dict[str, str] = {}
        for part in raw.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, _, val = part.partition("=")
            name, val = name.strip(), val.strip().strip('"')
            if name and val:
                cookies[name] = val
        return cookies

    def set_session_cookie(self, value: str | None) -> None:
        """Swap the signed-in session and drop every pooled connection."""
        self._session_cookie = (value or "").strip() or None
        self._cache.clear()
        self.close()

    @property
    def authenticated(self) -> bool:
        return bool(self.session_cookie)

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
            for name, value in self.parse_cookies(self.session_cookie).items():
                session.cookies.set(name, value, domain=".myfigurecollection.net")
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

    def _get(self, path: str, allow_missing: bool = False) -> tuple[str, str, int]:
        """Fetch a page. Returns (final_url, html, status).

        With ``allow_missing`` a 404 comes back as a normal result instead of
        raising, which matters for barcode lookups: MyFigureCollection hides
        adult entries from signed-out visitors behind a plain 404, but it still
        redirects to the right item first, and that redirect is worth keeping.
        """
        url = path if path.startswith("http") else BASE + path
        cached = self._cache.get(url)
        if cached and time.monotonic() - cached[0] < self.cache_ttl:
            return url, cached[1], 200

        self.breaker.check()
        self.bucket.acquire(timeout=60.0)
        from ..services import reqlog

        try:
            response = self.session.get(url)
        except Exception as exc:  # noqa: BLE001
            self.breaker.record_failure()
            reqlog.record("mfc", ok=False)
            raise MfcError(f"MyFigureCollection request failed: {exc}") from exc

        self.requests_made += 1
        reqlog.record("mfc", ok=response.status_code < 400)
        if response.status_code == 404:
            self.breaker.record_success()
            if not allow_missing:
                raise MfcNotFound("MyFigureCollection has no such page")
            return str(response.url), response.text, 404
        if response.status_code >= 400:
            self.breaker.record_failure()
            raise MfcError(f"MyFigureCollection returned {response.status_code}")

        self.breaker.record_success()
        final_url = str(response.url)
        self._cache[url] = (time.monotonic(), response.text)
        return final_url, response.text, response.status_code

    # -- lookup -----------------------------------------------------------
    def find_by_jan(self, jan: str) -> MfcItem | None:
        """Exact lookup by barcode. MFC redirects a matching search to the item."""
        jan = (jan or "").strip()
        if not jan.isdigit() or len(jan) < 8:
            return None

        final_url, html, status = self._get(
            f"/?keywords={quote(jan)}&_tb=item", allow_missing=True
        )
        match = _ITEM_ID_RE.search(final_url)
        if not match:
            # More than one hit, or none. Only accept an unambiguous redirect.
            return None

        mfc_id = int(match.group(1))
        if status == 404:
            # The redirect proves which entry the barcode belongs to, but the
            # page is withheld. Adult entries do this to signed-out visitors,
            # and the 404 carries no explanation. Keep the identification and
            # be honest that the tags are missing rather than discarding a
            # correct match.
            log.info("MFC entry %s is withheld from guests; linking without tags", mfc_id)
            return MfcItem(
                id=mfc_id,
                url=f"{BASE}/item/{mfc_id}",
                title="",
                restricted=True,
            )
        return self._parse_item(mfc_id, final_url, html)

    def get_item(self, mfc_id: int) -> MfcItem:
        final_url, html, status = self._get(f"/item/{mfc_id}", allow_missing=True)
        if status == 404:
            return MfcItem(
                id=mfc_id, url=f"{BASE}/item/{mfc_id}", title="", restricted=True
            )
        return self._parse_item(mfc_id, final_url, html)

    def search(self, keywords: str, root: int | None = ROOT_FIGURES) -> list[MfcListing]:
        """Keyword search, used when an item has no usable barcode."""
        keywords = (keywords or "").strip()
        if not keywords:
            return []
        path = f"/item/browse/figure/?keywords={quote(keywords)}"
        if root is None:
            path = f"/?_tb=item&keywords={quote(keywords)}"
        final_url, html, _status = self._get(path)

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
        _final_url, html, _status = self._get(path)

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

    #: Fetched to prove a session works. A restricted entry is the only
    #: reliable test, because everything else is visible to guests too.
    PROBE_RESTRICTED_ITEM = 166442

    def check_session(self) -> dict:
        """Report whether a signed-in session is configured and working."""
        if not self.authenticated:
            return {
                "configured": False,
                "valid": False,
                "username": None,
                "cookies": [],
                "detail": "No session cookie set. Restricted entries stay unreadable.",
            }

        cookies = self.parse_cookies(self.session_cookie)
        if "PHPSESSID" not in cookies:
            # Consent and analytics cookies sit right next to the session in
            # every cookie manager, and they are the ones people copy first.
            return {
                "configured": True,
                "valid": False,
                "username": None,
                "cookies": sorted(cookies),
                "detail": (
                    "No PHPSESSID among the pasted cookies"
                    + (f" (found: {', '.join(sorted(cookies))})" if cookies else "")
                    + ". PHPSESSID is the one that carries the sign-in; addtl_consent "
                    "and similar are cookie-banner leftovers."
                ),
            }

        try:
            _url, html, status = self._get("/", allow_missing=True)
        except MfcError as exc:
            return {"configured": True, "valid": False, "username": None, "detail": str(exc)}

        # A signed-in page carries a link to the member's own profile.
        match = re.search(r'href="/profile/([^"/]+)/?"', html)
        username = match.group(1) if match else None
        signed_in = bool(username) or "/session/signout" in html

        detail = (
            f"Signed in as {username}." if username
            else "Signed in." if signed_in
            else "The cookie was not accepted. Copy a fresh PHPSESSID from a signed-in browser."
        )

        result = {
            "configured": True,
            "valid": signed_in,
            "username": username,
            "cookies": sorted(cookies),
            "detail": detail,
        }

        if signed_in:
            # Confirm the session actually lifts the restriction, since being
            # signed in is not the same as being allowed to see adult entries.
            try:
                _u, _h, probe_status = self._get(
                    f"/item/{self.PROBE_RESTRICTED_ITEM}", allow_missing=True
                )
                result["restricted_entries_visible"] = probe_status == 200
                if probe_status != 200:
                    result["detail"] += (
                        " Restricted entries are still hidden; enable adult content in your"
                        " MyFigureCollection account settings."
                    )
            except MfcError:
                result["restricted_entries_visible"] = None
        return result

    def status(self) -> dict:
        return {
            "requests_made": self.requests_made,
            "rate_per_minute": self.bucket.rate_per_minute,
            "circuit": self.breaker.snapshot(),
            "cached_pages": len(self._cache),
            "authenticated": self.authenticated,
        }


client = MfcClient()
