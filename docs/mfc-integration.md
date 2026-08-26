# MyFigureCollection integration

AmiSearch cross-references catalogue items with MyFigureCollection so you can
browse a shop by MFC's tags — character, series, pose, outfit, sculptor.

## There is no usable API

- `api.php` and `api_v2.php` both return 404. The old XML/JSON API is gone.
- The community REST mirrors (`api.tenji.moe` and similar) return 500s.
- Keyword search on the site requires a session for some result types.

So this is a scraper, and it is written to be a polite one: **12 requests per
minute**, every page cached for an hour, one shared circuit breaker, and a
background job that trickles through the catalogue rather than bursting.

MFC also blocks stock Python TLS, so the same `curl_cffi` Chrome
impersonation is used as for AmiAmi.

## The pages that work

| Purpose | URL |
|---|---|
| Exact lookup by barcode | `/?keywords=<JAN>&_tb=item` |
| Item detail | `/item/<id>` |
| Browse by tag | `/?_tb=item&tags[]=<slug>&page=<n>&rootId=1` |
| Keyword search | `/item/browse/figure/?keywords=<terms>` |

A barcode search with exactly one match **redirects straight to the item
page**, which makes it an unambiguous lookup: if the final URL contains
`/item/<id>`, that is the match. Anything else is treated as no match rather
than guessed at.

Multiple `tags[]` parameters combine with **AND**. `rootId=1` narrows to
figures.

## Parsing an item page

Tags:

```html
<div class="object-tag">
  <a class="" href="/?_tb=item&amp;tags%5B%5D=armour" title="armour">armour</a>
  <a href="/tag/1183" class="information">?</a>
</div>
```

The slug is percent-encoded in the href (`1%2F7___` for the tag `1/7`), so it
is decoded on the way in and re-encoded when a URL is built. A `___` suffix
marks an automatically applied tag.

Entries — series, character, company, sculptor, materials — are `/entry/<id>`
links inside the labelled `data-field` rows.

## Matching strategy

1. **Barcode.** AmiAmi publishes `jancode` for almost every figure. An
   unambiguous redirect is an exact match, recorded with confidence `1.0`.
2. **Title search.** Scored by token overlap, weighted toward covering the
   shop's title — the MFC title legitimately carries extra words such as the
   company and the scale. Below `0.45` the match is rejected outright.

The match method is stored and surfaced in the UI, so a title match is shown
as *probable* rather than presented as fact.

## Why the tag index is local

Once an item is linked, its tags are copied into local `tags` and `item_tags`
tables. Discovery then queries those instead of MFC. This is faster, it means
every result is something you can actually buy right now, and it means
browsing by tag costs MFC nothing.

The "search MyFigureCollection" mode is the slower path that reaches figures
nobody on the instance has looked for yet. It looks up a bounded number of
shop matches per call and caches the seeds, so paging through results does
not re-fetch.
