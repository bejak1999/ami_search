"""AmiAmi provider.

AmiAmi runs an undocumented but stable JSON API behind the same Cloudflare
edge as the shop. It needs two things: an X-User-Key header and a browser
User-Agent. Everything the tracker needs is in there, so no HTML scraping.

Endpoints
---------
GET /api/v1.0/items   search, pagemax is capped at 50 upstream
GET /api/v1.0/item    full detail for one gcode

Filters confirmed against the live API:
  s_st_condition_flg=1              pre-owned only
  s_st_list_newitem_available=1     first-hand stock available
  s_st_list_preorder_available=1    pre-orderable
  s_st_list_backorder_available=1   back-orderable
  s_st_saleitem=1                   discounted
  s_cate_tag / s_maker_id / s_originaltitle_id
  s_sortkey=<key>                   ordering, see SORT_KEYS

A sort key without its direction suffix is accepted and then silently
ignored: asking for "price" returns the shop's default order, which looks
exactly like no sorting support at all. Only the suffixed forms do anything,
and the shop's own menu offers four of them under labels that do not say
which is which.

Price and release-date ranges are not supported upstream, so they are applied
locally after fetching.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import settings
from .base import (
    FacetOption,
    ItemNotFound,
    NormalizedItem,
    ProviderError,
    SearchQuery,
    SearchResult,
    ShopProvider,
)

log = logging.getLogger(__name__)

API_ROOT = "https://api.amiami.com/api/v1.0"
IMG_ROOT = "https://img.amiami.com"
SITE_ROOT = "https://www.amiami.com"

MAX_PER_PAGE = 50

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_SCALE_RE = re.compile(r"\b(1/\d{1,2})\b")
_GRADE_RE = re.compile(r"\(Pre-owned\s*([^)]*)\)", re.IGNORECASE)
_CODE_RE = re.compile(r"[A-Z]+[A-Z0-9-]*\d[A-Z0-9-]*")


#: AmiAmi grades a pre-owned item and its box separately, best first. The
#: order matters: it is what lets a watch say "Item:A or better".
GRADE_ORDER = ("S", "A", "B+", "B", "C", "D")

_GRADE_FIELD_RE = re.compile(
    r"(item|box)\s*[:：]\s*(S|A|B\+|B|C|D)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _clean_condition(sname: str | None) -> str:
    """Pull "Item:A Box:B" out of a pre-owned listing name."""
    match = _GRADE_RE.search(sname or "")
    return " ".join(match.group(1).split()) if match else ""


#: The red text on a product page, whatever it says.
_RED_TEXT = re.compile(r"<font[^>]*color[^>]*red[^>]*>(.*?)</font>", re.IGNORECASE | re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")

#: One red block holds several statements, one per line. Sampling the
#: catalogue turned up blocks mixing a bonus with a shipping notice - "Postcard
#: is included" and "*Shipping costs for this item may be very high" - so the
#: block has to be split before anything is judged, or one statement decides
#: the fate of the others.
_STATEMENT_BREAK = re.compile(r"(?:<br\s*/?>|[\r\n]|(?<=[.!?])\s+(?=\*))", re.IGNORECASE)

#: Words that mean a statement is about the parcel, not the copy in it.
_LOGISTICS = re.compile(
    r"shipping|shipped|shipment|package size|packaging size|delivered alone|"
    r"cannot be combined|combined with other items|delivery|dispatch|courier|"
    r"postage|freight|oversized|by air|air mail|sea mail",
    re.IGNORECASE,
)

#: AmiAmi marks shop-wide notices with a leading asterisk. Across fifteen
#: sampled listings every asterisked statement was shipping boilerplate and no
#: statement about a copy carried one, which generalises further than a list
#: of phrases: the shop can word a new notice however it likes and still mark
#: it the same way.
_SHOP_WIDE = re.compile(r"^\s*[*\u203b\u203b]")

#: Words that mean a statement is about this copy's condition after all. The
#: safety net on the two rules above: dropping a real fault is the failure
#: that matters, and a notice shown by mistake is only noise, so anything that
#: describes damage is kept whatever else it looks like.
_DAMAGE = re.compile(
    r"scratch|stain|discolo|yellow|sticky|broken|bent|crack|chip|peel|fade|"
    r"missing|detach|damage|dent|repair|loose|warp|mould|mold|rust|dirty|"
    r"deform|torn|tear|glue|paint transfer|droplet",
    re.IGNORECASE,
)

#: Something extra in the box. Written loosely on purpose: the shop says this
#: half a dozen ways, and mislabelling a bonus as a fault puts good news under
#: a warning triangle. Negations are excluded - "the stand is not included" is
#: a missing part, which is the opposite.
_BONUS = re.compile(
    r"^\s*(?:includes\b|comes with\b|bonus\b)|(?:is|are)\s+included\b",
    re.IGNORECASE,
)
_NOT_BONUS = re.compile(r"\bnot\b|\bno longer\b|\bwithout\b|\bmissing\b", re.IGNORECASE)


def is_shop_boilerplate(statement: str) -> bool:
    """Is this the shop talking about postage rather than about this copy?

    Eleven of nineteen sampled red passages were the one sentence every
    oversized item carries, so leaving them in buries the notes that say
    something particular. A statement is dropped when it is marked as a
    shop-wide notice or reads like one - unless it also describes damage, in
    which case it is kept regardless. Losing a fault is the expensive mistake;
    showing a notice is a cheap one.
    """
    text = statement.strip()
    if _DAMAGE.search(text):
        return False
    return bool(_SHOP_WIDE.match(text) or _LOGISTICS.search(text))


def is_bonus_note(statement: str) -> bool:
    """Is this line about something extra in the box rather than a fault?"""
    text = statement.strip()
    if _NOT_BONUS.search(text) or _DAMAGE.search(text):
        return False
    return bool(_BONUS.search(text))


def shop_notes(remarks: str | None) -> list[dict]:
    """The red statements, each tagged as a fault or a bonus.

    Same rule as :func:`condition_note`, which returns the joined text; this
    returns the parts so the interface can show a missing pencil board and an
    included poster differently instead of putting both under one heading.
    """
    note = condition_note(remarks)
    if not note:
        return []
    return [
        {"text": part, "kind": "bonus" if is_bonus_note(part) else "fault"}
        for part in note.split(" \u00b7 ")
    ]


def condition_note(remarks: str | None) -> str | None:
    """What the shop says about this copy, beyond its grade.

    AmiAmi prints these in red on the product page and they are the only place
    some of it appears - a fault that explains an otherwise startling price, or
    a bonus that comes with the copy:

        The pencil board is missing.                    a fault
        White area on the skirt has become yellowish    a fault
        Outfit is sticky and has droplets due to age    a fault
        Poster is included.                             a bonus
        Postcard is included
        Right rabbit ear is detached                    one of each

    Both kinds are kept. The point is not only to explain a low price; it is
    that this is information which otherwise lives on AmiAmi's page and
    nowhere else.

    Shipping notices are dropped, and they are the reason any filtering
    happens at all:

        *Shipping costs for this item may be very high due to package size

    Eleven of the nineteen red passages sampled were that one sentence, which
    every oversized item carries. Repeating it on all of them buries the
    handful of notes that say something particular about a copy.

    Judged statement by statement rather than block by block, because a block
    can hold one of each - "Postcard is included" arrives above both a
    detached rabbit ear and a shipping warning - and a verdict on the block
    would take the wrong ones with it. They are separated by a newline;
    fifteen raw captures confirm that and none used a <br>.
    """
    if not remarks:
        return None
    kept: list[str] = []
    for block in _RED_TEXT.findall(remarks):
        for statement in _STATEMENT_BREAK.split(block):
            text = " ".join(_HTML_TAG.sub("", statement).split())
            if not text or is_shop_boilerplate(text):
                continue
            kept.append(text)
    return " · ".join(kept) or None


def parse_grades(text: str | None) -> tuple[str | None, str | None]:
    """Split a condition string into (item grade, box grade).

    Handles both the detail form "(Pre-owned ITEM:A/BOX:B)..." and the
    other_items form "Condition Item:A　Box:B", including the ideographic
    space AmiAmi uses between the two.
    """
    if not text:
        return None, None
    grades: dict[str, str] = {}
    for match in _GRADE_FIELD_RE.finditer(text):
        grades[match.group(1).lower()] = match.group(2).upper()
    return grades.get("item"), grades.get("box")


def grade_rank(grade: str | None) -> int:
    """Lower is better. Unknown grades sort last so they never pass a filter."""
    if not grade:
        return len(GRADE_ORDER)
    try:
        return GRADE_ORDER.index(grade.upper())
    except ValueError:
        return len(GRADE_ORDER)


def meets_grade(grade: str | None, minimum: str | None) -> bool:
    """True when ``grade`` is at least as good as ``minimum``."""
    if not minimum:
        return True
    return grade_rank(grade) <= grade_rank(minimum)


#: Our sort names mapped onto the shop's, so the shop does the ordering
#: across the whole result rather than us reordering the fifty rows we happen
#: to be holding. Anything absent here has no upstream equivalent and is
#: sorted locally, which only ever orders the current page.
#:
#: What AmiAmi's own dropdown offers, with the names it gives them. Two of
#: these - releasedatea and buy_priced - are commented out in the shop's
#: markup, so a visitor is not offered them, but the API honours both.
#:
#: The one that matters is "preowned" (中古). Measured against 594 known
#: arrivals, its first twenty pages held 300 of them, 5.35 times what a random
#: slice of that size would catch. "regtimed" (新着順, "newest") held nine -
#: 0.20x, worse than a shuffle - because it orders by when the *product
#: record* was registered, not by when a used copy was taken in, so a copy
#: arriving today attaches to a record made years ago and never moves to the
#: front. An earlier note here claimed the opposite on the strength of twelve
#: products on one page; two longitudinal runs say otherwise.
#:
#: "buy_priced" is AmiAmi's buyback price, highest first - what the shop will
#: pay for a figure. Nothing reads it yet; it is listed so the next person
#: knows it exists.
SORT_KEYS: dict[str, str] = {
    "newest": "regtimed",
    "updated": "preowned",
    "preowned": "preowned",
    "release": "releasedated",
    "release_asc": "releasedatea",
    "oldest": "regtimea",
    "price_asc": "pricea",
    "price_desc": "priced",
    "buyback": "buy_priced",
}

#: "priced" returns dear listings but not in order, so the page it hands back
#: is re-sorted locally. That still beats sorting an arbitrary page: these are
#: the expensive listings from the whole result, just shuffled.
SORT_KEYS_NEEDING_LOCAL_SORT = frozenset({"price_desc"})

#: One second-hand copy, as opposed to the product it is a copy of. AmiAmi
#: numbers them per product and counts up for ever, so "-R124" is the 124th
#: used copy of FIGURE-184067 the shop has taken in, and "-R" with nothing
#: after it is the product those copies hang under.
_COPY_CODE = re.compile(r"-R\d+$")


def is_copy_code(code: str) -> bool:
    """Is this an scode - one graded copy - rather than a product gcode?"""
    return bool(_COPY_CODE.search(code or ""))


class AmiAmiProvider(ShopProvider):
    id = "amiami"
    name = "AmiAmi"
    home_url = SITE_ROOT
    currency = "JPY"
    supports_facets = True
    description = "Japanese hobby shop, new and pre-owned figures, JPY pricing."

    def default_headers(self) -> dict[str, str]:
        headers = super().default_headers()
        headers.update(
            {
                "X-User-Key": settings.amiami_api_key,
                "Origin": SITE_ROOT,
                "Referer": SITE_ROOT + "/",
            }
        )
        return headers

    # -- URLs -------------------------------------------------------------
    def product_url(self, code: str) -> str:
        return SITE_ROOT + "/eng/detail/?gcode=" + code

    def parse_url(self, url: str) -> str | None:
        """Pull a gcode out of any AmiAmi product link the user pastes."""
        candidate = (url or "").strip()
        if not candidate:
            return None
        if "amiami" not in candidate.lower():
            # Bare product codes are accepted too.
            if _CODE_RE.fullmatch(candidate.upper()):
                return candidate.upper()
            return None
        parsed = urlparse(candidate if "//" in candidate else "https://" + candidate)
        qs = parse_qs(parsed.query)
        for key in ("gcode", "scode"):
            if qs.get(key):
                return qs[key][0]
        match = re.search(r"gcode=([A-Za-z0-9_-]+)", candidate)
        if match:
            return match.group(1)
        match = re.search(r"([A-Z]{3,}-[A-Z0-9-]+)", candidate.upper())
        return match.group(1) if match else None

    @staticmethod
    def normalise_watch_code(code: str) -> str:
        """Reduce a product code to something worth watching long-term.

        A code names a listing, not a product. FIGURE-x is the first-hand
        listing and FIGURE-x-R the pre-owned one, but a link copied after
        clicking a buying choice carries a third form, FIGURE-x-R032, which
        identifies one specific graded copy. Watching that would stop working
        the moment that copy sells, which is exactly when the watch mattered.
        """
        cleaned = (code or "").strip().upper()
        if not cleaned:
            return cleaned
        # Strip the per-copy suffix, keeping the pre-owned marker itself.
        return re.sub(r"-R\d+$", "-R", cleaned)

    @staticmethod
    def _abs_image(path: str | None) -> str | None:
        if not path:
            return None
        if path.startswith("http"):
            return path
        return IMG_ROOT + path

    # -- Search -----------------------------------------------------------
    def _build_params(self, query: SearchQuery) -> dict[str, Any]:
        params: dict[str, Any] = {
            "pagecnt": max(1, query.page),
            "pagemax": min(max(1, query.per_page), MAX_PER_PAGE),
            "lang": "eng",
        }
        if query.keywords:
            params["s_keywords"] = query.keywords

        if query.condition == "preowned":
            params["s_st_condition_flg"] = 1
        elif query.condition == "new":
            # There is no "new only" flag upstream; ask for items with
            # first-hand stock and drop any -R listing when normalizing.
            params["s_st_list_newitem_available"] = 1

        if query.stock_filter == "in_stock" and query.condition != "preowned":
            params["s_st_list_newitem_available"] = 1
        elif query.stock_filter == "preorder":
            params["s_st_list_preorder_available"] = 1
        elif query.stock_filter == "backorder":
            params["s_st_list_backorder_available"] = 1

        sort_key = SORT_KEYS.get(query.sort)
        if sort_key:
            params["s_sortkey"] = sort_key

        if query.category_id:
            params["s_cate_tag"] = query.category_id
        if query.maker_id:
            params["s_maker_id"] = query.maker_id
        if query.series_id:
            params["s_originaltitle_id"] = query.series_id
        if query.extra.get("on_sale"):
            params["s_st_saleitem"] = 1
        if query.extra.get("store_bonus"):
            params["s_st_list_store_bonus"] = 1

        params.update(query.extra.get("raw_params") or {})
        return params

    def search(self, query: SearchQuery) -> SearchResult:
        started = time.monotonic()
        params = self._build_params(query)
        response = self.request("GET", API_ROOT + "/items", params=params)
        payload = self._decode(response)

        raw_items = payload.get("items") or []
        items = [self._normalize_list_item(raw) for raw in raw_items]
        items = self._apply_local_filters(items, query)
        items = self._apply_local_sort(items, query.sort)

        total = int((payload.get("search_result") or {}).get("total_results") or 0)
        return SearchResult(
            items=items,
            total=total,
            page=query.page,
            per_page=params["pagemax"],
            facets=self._extract_facets(payload),
            took_ms=int((time.monotonic() - started) * 1000),
        )

    def get_item(self, code: str) -> NormalizedItem:
        """Look up one product or one graded copy.

        These are different keys and neither substitutes for the other. A
        product is a gcode (FIGURE-184067-R); one second-hand copy of it is an
        scode (FIGURE-184067-R124). Asked for a copy under "gcode" the API
        answers that it has no such item, and asked for a product under
        "scode" it rejects the request outright - so a watch on a copy failed
        on every single poll and reported the shop had deleted the listing.

        The parameter is chosen from the shape of the code, and the other one
        is tried if that turns out wrong: the pattern covers what AmiAmi
        issues today, and guessing wrong should cost a request rather than an
        answer.
        """
        first = "scode" if is_copy_code(code) else "gcode"
        for key in (first, "gcode" if first == "scode" else "scode"):
            try:
                response = self.request(
                    "GET", API_ROOT + "/item", params={key: code, "lang": "eng"}
                )
            except ProviderError:
                # "Invalid Request 22" is what a gcode passed as scode gets.
                continue
            payload = self._decode(response)
            raw = payload.get("item")
            if raw:
                return self._normalize_detail(raw, payload.get("_embedded") or {})
        raise ItemNotFound("AmiAmi has no item with code " + code)

    def suggest(self, term: str) -> list[FacetOption]:
        """Category suggestions, used by the narrow-down UI."""
        result = self.search(SearchQuery(keywords=term, per_page=1))
        return [
            FacetOption(id=f["id"], name=f["name"], count=f.get("count"))
            for f in result.facets.get("categories", [])
        ]

    # -- Decoding ---------------------------------------------------------
    @staticmethod
    def _decode(response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(
                "AmiAmi returned a non-JSON body, usually a Cloudflare challenge"
            ) from exc
        if not payload.get("RSuccess", False):
            message = " ".join((payload.get("RMessage") or "unknown error").split())
            # When a pre-owned listing sells out AmiAmi deletes it outright and
            # answers "Invalid Request 21". That is a normal, expected outcome
            # for a tracker, not an upstream fault, so it must not trip the
            # circuit breaker or look like an error in the logs.
            if "21" in message and "Invalid Request" in message:
                raise ItemNotFound("AmiAmi no longer lists this item")
            raise ProviderError("AmiAmi rejected the request: " + message)
        return payload

    @staticmethod
    def _extract_facets(payload: dict[str, Any]) -> dict[str, Any]:
        embedded = payload.get("_embedded") or {}
        out: dict[str, Any] = {}
        pairs = (
            ("category_tags", "categories"),
            ("makers", "makers"),
            ("original_titles", "series"),
            ("character_names", "characters"),
        )
        for src, dst in pairs:
            values = embedded.get(src) or []
            if values:
                out[dst] = [
                    {"id": v.get("id"), "name": v.get("name"), "count": v.get("count")}
                    for v in values
                ]
        return out

    @classmethod
    def _parse_release(cls, value: str | None) -> datetime | None:
        """AmiAmi mixes two release formats in one API: Apr-2023 and ISO."""
        if not value:
            return None
        value = value.strip()
        try:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", value):
                return datetime.strptime(value[:10], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            match = re.fullmatch(r"([A-Za-z]{3})-(\d{4})", value)
            if match:
                month = _MONTHS.get(match.group(1).lower())
                if month:
                    return datetime(int(match.group(2)), month, 1, tzinfo=timezone.utc)
        except ValueError:
            return None
        return None

    @classmethod
    def _release_label(cls, raw_value: str | None) -> str | None:
        """One display form for release dates.

        The list endpoint returns ISO timestamps and the detail endpoint
        returns "Apr-2023", so normalize both to the short, month-precision
        form the shop itself shows.
        """
        parsed = cls._parse_release(raw_value)
        if parsed is None:
            return raw_value or None
        return parsed.strftime("%b-%Y")

    @staticmethod
    def _first(values: Any) -> str | None:
        if isinstance(values, list) and values:
            entry = values[0]
            if isinstance(entry, dict):
                return entry.get("name")
            return str(entry)
        return None

    @staticmethod
    def _scale_from_name(name: str) -> str | None:
        match = _SCALE_RE.search(name or "")
        return match.group(1) if match else None

    @staticmethod
    def _scale_from_spec(spec: str | None) -> str | None:
        if not spec:
            return None
        match = re.search(r"Scale:\s*(\S+)", spec)
        return match.group(1) if match else None

    # -- Normalization ----------------------------------------------------
    def _normalize_list_item(self, raw: dict[str, Any]) -> NormalizedItem:
        code = raw.get("gcode") or ""
        name = raw.get("gname") or ""
        preowned = bool(raw.get("condition_flg")) or code.endswith("-R")
        # min_price is the cheapest grade on offer, max_price the dearest.
        # A target price has to be judged against the cheapest one.
        price = raw.get("min_price")
        price_max = raw.get("max_price")
        thumb = self._abs_image(raw.get("thumb_url"))
        return NormalizedItem(
            provider=self.id,
            code=code,
            name=name,
            url=self.product_url(code),
            currency=self.currency,
            price=float(price) if price else None,
            price_max=float(price_max) if price_max else None,
            list_price=float(raw["c_price_taxed"]) if raw.get("c_price_taxed") else None,
            maker=raw.get("maker_name"),
            scale=self._scale_from_name(name),
            jan_code=raw.get("jancode"),
            image_url=thumb,
            images=[thumb] if thumb else [],
            condition="preowned" if preowned else "new",
            in_stock=bool(raw.get("instock_flg")),
            is_preorder=bool(raw.get("preorderitem") or raw.get("list_preorder_available")),
            is_backorder=bool(raw.get("list_backorder_available")),
            order_closed=bool(raw.get("order_closed_flg")),
            sale_status=raw.get("salestatus"),
            release_date=self._release_label(raw.get("releasedate")),
            release_date_parsed=self._parse_release(raw.get("releasedate")),
            detail_loaded=False,
            raw=raw,
        )

    @staticmethod
    def _collect_variants(raw: dict[str, Any], embedded: dict[str, Any]) -> list[dict[str, Any]]:
        """Every buyable sub-listing under one product code.

        AmiAmi sells a pre-owned product as several graded copies at different
        prices. The detail endpoint returns one of them in the top-level
        fields and the rest in ``_embedded.other_items`` - this is the
        "More Buying Choices" box on the product page. The one it picks is not
        the cheapest, so taking the top-level price at face value can be tens
        of thousands of yen off.
        """
        seen: dict[str, dict[str, Any]] = {}

        def record(code: str, price: Any, condition: str, note: str | None = None) -> None:
            if not code or not price or code in seen:
                return
            item_grade, box_grade = parse_grades(condition)
            seen[code] = {
                "code": code,
                "price": float(price),
                "condition": " ".join((condition or "").split()),
                "item_grade": item_grade,
                "box_grade": box_grade,
                "note": note,
            }

        # The note belongs to this one copy and to no other. FIGURE-045661-R514
        # is an ITEM:C at 3,920 with "[Discoloration] Upper body skin area has
        # become white"; R515 is an ITEM:A at 9,780 with nothing said about it
        # at all. Attaching the note to the product would put the first copy's
        # stains on the second one's listing.
        #
        # Which copy the shop hands back under a product code is its own
        # choice and not always the cheapest - FIGURE-184067-R answers with a
        # 38,680 copy while the cheapest is 34,380 - so the note is filed
        # against the scode in the response rather than against a position.
        record(
            raw.get("scode") or raw.get("gcode") or "",
            raw.get("price") or raw.get("price1"),
            _clean_condition(raw.get("sname")),
            condition_note(raw.get("remarks")),
        )
        for entry in embedded.get("other_items") or []:
            record(entry.get("scode") or "", entry.get("price"), entry.get("condition") or "")

        # Cheapest first, then best condition, which is the order someone
        # actually shops in. Copies from other_items carry no note because the
        # shop does not return one there - only silence, which is not the same
        # as knowing there is nothing to say.
        return sorted(
            seen.values(),
            key=lambda v: (v["price"], grade_rank(v["item_grade"]), grade_rank(v["box_grade"])),
        )

    def _normalize_detail(
        self, raw: dict[str, Any], embedded: dict[str, Any]
    ) -> NormalizedItem:
        code = raw.get("gcode") or ""
        name = raw.get("gname") or raw.get("sname_simple") or ""
        preowned = bool(raw.get("condition_flg")) or code.endswith("-R")

        images: list[str] = []
        main = self._abs_image(raw.get("main_image_url"))
        if main:
            images.append(main)
        for group in ("review_images", "bonus_images"):
            for entry in embedded.get(group) or []:
                url = self._abs_image(entry.get("image_url"))
                if url and url not in images:
                    images.append(url)

        variants = self._collect_variants(raw, embedded)
        # The top-level price is one arbitrary grade among several. Reporting
        # it would make an item watch compare against the wrong number, so the
        # cheapest listing wins and the range is kept alongside it.
        cheapest = variants[0] if variants else None
        dearest = variants[-1] if variants else None

        grade = (cheapest or {}).get("condition") or _clean_condition(raw.get("sname")) or None
        price = (cheapest or {}).get("price") or raw.get("price") or raw.get("price1")
        price_max = (dearest or {}).get("price") if dearest else None
        # Do not trust soldout_flg here: the detail endpoint returns 1 for
        # every item, including freshly opened pre-orders. The usable signals
        # are stock / order_closed_flg / end_flg, plus cart_type
        # (8 = pre-order, 9 = ships now).
        is_preorder = bool(raw.get("preorderitem")) or raw.get("cart_type") == 8
        closed = bool(raw.get("order_closed_flg")) or bool(raw.get("end_flg"))
        available = bool(raw.get("stock")) and not closed

        return NormalizedItem(
            provider=self.id,
            code=code,
            name=name,
            name_jp=raw.get("sname_simple_j"),
            url=self.product_url(code),
            currency=self.currency,
            price=float(price) if price else None,
            price_max=float(price_max) if price_max else None,
            variants=variants,
            list_price=float(raw["c_price_taxed"]) if raw.get("c_price_taxed") else None,
            maker=raw.get("maker_name") or self._first(embedded.get("makers")),
            series=self._first(embedded.get("original_titles")),
            character=self._first(embedded.get("character_names")),
            scale=self._scale_from_name(name) or self._scale_from_spec(raw.get("spec")),
            jan_code=raw.get("jancode"),
            image_url=images[0] if images else None,
            images=images,
            condition="preowned" if preowned else "new",
            condition_grade=grade,
            in_stock=available and not is_preorder,
            is_preorder=is_preorder and available,
            is_backorder=bool(raw.get("backorderitem")),
            order_closed=closed,
            sale_status=raw.get("salestatus"),
            release_date=self._release_label(raw.get("releasedate")),
            release_date_parsed=self._parse_release(raw.get("releasedate")),
            spec=raw.get("spec"),
            remarks=raw.get("remarks") or raw.get("memo"),
            # The product-level note is the one on the copy the shop
            # answered with, kept for the header and for search. Which copy
            # that is lives in the variant it came from.
            condition_note=condition_note(raw.get("remarks")),
            condition_note_code=raw.get("scode") or None,
            shop_notes=shop_notes(raw.get("remarks")),
            detail_loaded=True,
            raw=raw,
        )

    # -- Local filtering and sorting --------------------------------------
    @staticmethod
    def _apply_local_filters(
        items: list[NormalizedItem], query: SearchQuery
    ) -> list[NormalizedItem]:
        out = items
        if query.condition == "new":
            out = [i for i in out if i.condition == "new"]
        elif query.condition == "preowned":
            out = [i for i in out if i.condition == "preowned"]

        if query.stock_filter == "in_stock":
            out = [i for i in out if i.in_stock]
        elif query.stock_filter == "preorder":
            out = [i for i in out if i.is_preorder]
        elif query.stock_filter == "backorder":
            out = [i for i in out if i.is_backorder]

        if query.min_price is not None:
            out = [i for i in out if i.price is not None and i.price >= query.min_price]
        if query.max_price is not None:
            out = [i for i in out if i.price is not None and i.price <= query.max_price]

        if query.exclude_keywords:
            lowered = [k.lower().strip() for k in query.exclude_keywords if k.strip()]
            out = [i for i in out if not any(k in i.name.lower() for k in lowered)]
        return out

    @staticmethod
    def _apply_local_sort(items: list[NormalizedItem], sort: str) -> list[NormalizedItem]:
        """Order the fetched page, for the orderings the shop cannot do.

        Only reached when SORT_KEYS has no upstream equivalent, or when the
        upstream one is known not to order cleanly. Sorting here can only ever
        arrange the fifty rows in hand, so "cheapest" would mean "cheapest on
        this page" - which is why as much of this as possible now happens at
        the shop instead.
        """
        if sort in SORT_KEYS and sort not in SORT_KEYS_NEEDING_LOCAL_SORT:
            return items

        epoch = datetime.min.replace(tzinfo=timezone.utc)
        if sort == "price_asc":
            return sorted(items, key=lambda i: (i.price is None, i.price or 0))
        if sort == "price_desc":
            return sorted(items, key=lambda i: (i.price is None, -(i.price or 0)))
        if sort == "release":
            return sorted(
                items,
                key=lambda i: i.release_date_parsed or epoch,
                reverse=True,
            )
        if sort == "discount":
            def discount(i: NormalizedItem) -> float:
                if not i.list_price or not i.price:
                    return 0.0
                return 1.0 - (i.price / i.list_price)

            return sorted(items, key=discount, reverse=True)
        return items
