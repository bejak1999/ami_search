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
  s_sortkey=preowned                pre-owned listings first

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

        if query.sort == "preowned":
            params["s_sortkey"] = "preowned"

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
        response = self.request(
            "GET", API_ROOT + "/item", params={"gcode": code, "lang": "eng"}
        )
        payload = self._decode(response)
        raw = payload.get("item")
        if not raw:
            raise ItemNotFound("AmiAmi has no item with code " + code)
        return self._normalize_detail(raw, payload.get("_embedded") or {})

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
        price = raw.get("min_price")
        thumb = self._abs_image(raw.get("thumb_url"))
        return NormalizedItem(
            provider=self.id,
            code=code,
            name=name,
            url=self.product_url(code),
            currency=self.currency,
            price=float(price) if price else None,
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

        grade = None
        grade_match = _GRADE_RE.search(raw.get("sname") or "")
        if grade_match:
            grade = (grade_match.group(1) or "").strip() or None

        price = raw.get("price") or raw.get("price1")
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
        # Upstream only honours its default order and preowned-first, so any
        # other ordering is applied to the page we just fetched.
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
