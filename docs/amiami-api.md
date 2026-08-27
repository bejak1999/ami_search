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

## One product code, several prices

This is the detail that breaks a naive tracker.

A pre-owned product is sold as **several graded copies at once**, each its own
sub-listing with its own `scode` and price. The product page shows them as
"More Buying Choices". `FIGURE-165063-R` is a live example:

| scode | Price | Condition |
|---|---|---|
| `FIGURE-165063-R151` | 40,180 JPY | Item:B Box:B |
| `FIGURE-165063-R153` | 53,980 JPY | Item:B+ Box:B |
| `FIGURE-165063-R152` | 53,980 JPY | Item:B+ Box:B |
| `FIGURE-165063-R156` | 59,980 JPY | Item:A Box:B |
| `FIGURE-165063-R155` | 59,980 JPY | Item:A Box:B |

The two endpoints disagree about which price to show:

- **Search** returns `min_price` (40,180) and `max_price` (59,980) — the range.
- **Detail** returns one arbitrary sub-listing in its top-level fields. For
  this item that is `R156` at **59,980**, the dearest. The rest are in
  `_embedded.other_items`.

Taking the detail price at face value therefore reports a figure **19,800 JPY
too high**, and a watch targeting 45,000 would never fire even though a copy is
listed at 40,180.

AmiSearch reads `_embedded.other_items` together with the primary `scode`,
sorts them by price, and reports the **cheapest** as the item price with the
dearest as `price_max`. Grades are parsed out of the condition strings, which
appear in two shapes:

```
"(Pre-owned ITEM:A/BOX:B)High School D x D HERO Rias Gremory…"   # detail sname
"Condition Item:B+　Box:B"                                    # other_items
```

Note the ideographic space (U+3000) between the two fields in the second form.

Grades run `S` > `A` > `B+` > `B` > `C` > `D`. A watch can set a minimum for
the figure and the box independently; the target price is then compared
against the cheapest copy that meets both, and an item whose copies are all
below the minimum simply does not match.

Because search responses carry only the range and never the grades, a search
watch with a grade filter opens the detail page for a bounded number of
candidates per run — the cheapest ones that are still plausibly within reach
of the target. The rest resolve on later polls.

## A product code names a listing, not a product

Three forms of code look alike and mean different things:

| Code | What it is |
|---|---|
| `FIGURE-180385` | The first-hand listing |
| `FIGURE-180385-R` | The pre-owned listing, covering every graded copy |
| `FIGURE-180385-R032` | One specific graded copy of the pre-owned listing |

The third form is what a link carries after clicking a buying choice, and the
number changes with the choice. Watching it would stop working the instant
that particular copy sold, which is exactly when the watch mattered, so
`normalise_watch_code` reduces it back to the listing.

Entering only the base code on AmiAmi produces "We could not find the exact
item. Here is an item that might be the same as the one you are looking for",
because the shop is resolving a code that has no listing of its own in the
condition being browsed.

This is why an item watch has no condition filter. A condition filter over a
single listing would be a setting that silently does nothing; the listing
itself is the condition, so the watch editor switches the code instead.

The two listings share a product image: the file is named after the base code,
so `FIGURE-180385.jpg` serves both. A listing being deleted and relisted
therefore reuses the same cached photo rather than replacing it. A genuinely
different picture means a different URL, which becomes a separate cache entry;
nothing is overwritten in place.
