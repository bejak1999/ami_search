# The AmiAmi API

Notes from reverse-engineering the endpoints AmiSearch depends on. Everything
here was verified against the live API; if AmiAmi changes something, this is
the page to re-check first.

There is no official documentation and no official permission. The endpoints
below are the ones the shop's own frontend calls.

## Authentication and transport

```http
GET https://api.amiami.com/api/v1.0/items
X-User-Key: amiami_dev
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 …
Origin: https://www.amiami.com
Referer: https://www.amiami.com/
```

`X-User-Key` is required, and `amiami_dev` is the value the site itself uses.

### Cloudflare blocks on the TLS fingerprint

This is the single most important detail. A request from `requests`, `httpx`
or any other stock Python HTTP client is rejected with **403** before a single
header is examined — the JA3/JA4 fingerprint of Python's TLS handshake is
enough. Adding headers does not help, and neither does HTTP/2.

`curl` succeeds where Python fails, which is what points at the handshake
rather than the request. AmiSearch therefore uses
[`curl_cffi`](https://github.com/lexiforest/curl_cffi) with
`impersonate="chrome"`, which presents a real Chrome handshake.

```python
from curl_cffi import requests

session = requests.Session(impersonate="chrome")
```

**libcurl handles are not thread-safe.** AmiSearch keeps one session per
thread in `threading.local()`, because the same provider object is used from
both the scheduler's worker pool and FastAPI's request threadpool. Sharing one
session across threads produces sporadic 20-second timeouts that look like the
upstream is down.

## Search

```
GET /api/v1.0/items
```

| Parameter | Meaning |
|---|---|
| `s_keywords` | Free text, matched against the English product title |
| `pagecnt` | Page number, 1-based |
| `pagemax` | Results per page. **Capped at 50**; 60 returns `Invalid Request` |
| `lang` | `eng` or `jpn` |
| `s_st_condition_flg=1` | Pre-owned listings only |
| `s_st_list_newitem_available=1` | Items with first-hand stock |
| `s_st_list_preorder_available=1` | Pre-orderable |
| `s_st_list_backorder_available=1` | Back-orderable |
| `s_st_saleitem=1` | Discounted |
| `s_st_list_store_bonus=1` | Comes with a store bonus |
| `s_cate_tag` | Category tag id, from `_embedded.category_tags` |
| `s_maker_id` | Manufacturer id |
| `s_originaltitle_id` | Series id |
| `s_sortkey=preowned` | Pre-owned listings first |

### Not supported

Price ranges, release-date ranges and character-id filters are silently
ignored — the response comes back unfiltered, which is worse than an error
because it looks like it worked. `s_sortkey` accepts several values but only
`preowned` changes the ordering; everything else returns the default order
(newest first). AmiSearch applies price filters, exclusions and the remaining
sort orders locally, to the page it fetched.

### Response

```json
{
  "RSuccess": true,
  "RMessage": "OK",
  "search_result": { "total_results": 7715 },
  "items": [
    {
      "gcode": "FIGURE-207209",
      "gname": "Nendoroid Slow Damage Towa",
      "thumb_url": "/images/product/main/263/FIGURE-207209.jpg",
      "min_price": 6210,
      "max_price": 6210,
      "c_price_taxed": 6900,
      "maker_name": "Orange Rouge",
      "condition_flg": 0,
      "instock_flg": 0,
      "list_preorder_available": 1,
      "order_closed_flg": 0,
      "releasedate": "2027-02-28 00:00:00",
      "jancode": "4570232591868"
    }
  ],
  "_embedded": {
    "category_tags": [{ "id": 1, "name": "Figures Categories", "count": 5377 }]
  }
}
```

Images are served from `https://img.amiami.com` + `thumb_url`.

`min_price` and `max_price` differ for pre-owned items, because one `gcode`
covers several sub-listings at different condition grades.

## Item detail

```
GET /api/v1.0/item?gcode=FIGURE-153570-R&lang=eng
```

Returns the full record: English and Japanese titles, the spec sheet,
`jancode`, the review-image gallery, and `_embedded` entries for maker, series
and character.

### Availability flags are a trap

The detail endpoint returns **`soldout_flg: 1` for every item**, including
pre-orders that opened this morning, and **`instock_flg: 0` for every item**.
Both fields are dead on this endpoint. Trusting either one means every item
looks sold out.

The flags that actually work:

| Field | Meaning |
|---|---|
| `stock` | `1` when the item is orderable |
| `order_closed_flg` | `1` when ordering has closed |
| `end_flg` | `1` when the listing has ended |
| `cart_type` | `8` = pre-order, `9` = ships now |
| `preorderitem` | `1` for a pre-order |
| `salestatus` | `"Pre-order"`, `"Released"`, … |

So availability is:

```python
is_preorder = bool(raw["preorderitem"]) or raw["cart_type"] == 8
closed      = bool(raw["order_closed_flg"]) or bool(raw["end_flg"])
available   = bool(raw["stock"]) and not closed
in_stock    = available and not is_preorder
```

On the **search** endpoint, by contrast, `instock_flg` is reliable and means
"ships immediately".

### Sold-out pre-owned listings are deleted

When a pre-owned listing sells, AmiAmi removes it rather than flagging it:

```json
{ "RSuccess": false, "RMessage": "Invalid Request\n21" }
```

For a price tracker this is *information*, not a fault. AmiSearch maps it to
`ItemNotFound`, records a final "sold out" point in the price history, and
deliberately does **not** let it trip the circuit breaker or appear as an
error in the logs.

It is also why the app caches everything it sees: once a listing is gone, the
only remaining record of what it cost is the one you kept.

## Release date formats

The two endpoints disagree. Search returns `"2027-02-28 00:00:00"`; detail
returns `"Apr-2023"`. AmiSearch parses both and presents the short,
month-precision form everywhere, since that is the precision AmiAmi itself
publishes.

## Rate limiting

No documented limit, and no `Retry-After` header. Sustained bursts produce
20-second timeouts rather than a clean 429.

AmiSearch runs a process-wide token bucket, default 40 requests per minute
shared across every watch, with jitter, at most 3 concurrent requests, and a
circuit breaker that backs off exponentially after repeated failures. That has
been comfortable in practice. Raising it makes alerts faster and a temporary
block more likely.
