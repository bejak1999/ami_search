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

## Entries MFC withholds

Some entries are visible only to signed-in members, chiefly adult ones. For a
signed-out visitor MFC does not say so: it serves a plain **404 Not Found**
with no explanation. The "18+" marker only renders for members who have
enabled adult content.

This matters because the barcode search still redirects correctly first:

```
GET /?keywords=4562177700078&_tb=item
  -> 302 to /item/166442
  -> 404
```

The redirect target is authoritative even though the page is withheld, so the
identification is kept rather than thrown away. Such items are linked with
`mfc_restricted = true`, get their MFC link in the UI, and carry no tags. The
item page says why instead of looking broken.

### Using a signed-in session

Supplying a `PHPSESSID` from a signed-in browser makes those entries readable.
Set `MFC_SESSION_COOKIE`, or paste it in **Administration** where it takes
effect without a restart.

#### Getting the cookie

1. **Sign in to MyFigureCollection and tick *remember me*.** Without it the
   session is browser-session-scoped and dies when you close the browser,
   taking the pasted value with it.
2. **Enable adult content** under `Settings → Account → Content`. Being signed
   in is not sufficient on its own; that switch is what lifts the restriction.
   The admin view checks the two separately and will say so.
3. **Open the cookie inspector.** `F12`, then `Storage` in Firefox or
   `Application` in Chrome, then `Cookies → https://myfigurecollection.net`.
4. **Copy the value of the row named `PHPSESSID`.** Not `addtl_consent`, not
   `euconsent-v2`, not `rzr_seg` — those are cookie-banner leftovers that sit
   immediately next to it.
5. Paste it into Administration. A bare value, `PHPSESSID=…`, or an entire
   `k=v; k=v` cookie header are all accepted, and any extra cookies are kept
   in case the session ever rests on more than one.

#### If you run a cookie cleaner

Extensions such as Cookie AutoDelete remove cookies when a tab closes. Most of
MyFigureCollection's cookies are session-scoped to begin with, so a cleaner
will typically take the sign-in with them and the value stored here goes stale
within hours. Allow-list `myfigurecollection.net` in the extension before
extracting the cookie.

#### When it stops working

`GET /api/admin/mfc/session` reports the failure mode rather than a bare
false: no `PHPSESSID` among the pasted cookies, a cookie that was not accepted,
or a valid session whose account still has adult content switched off.

A cookie is taken rather than a username and password deliberately:

* the account password never reaches this database
* signing out on MyFigureCollection revokes it immediately
* no login flow has to be replayed against a CSRF-protected form, which would
  be fragile and would look far more like automation than a normal session

`GET /api/admin/mfc/session` reports whether the cookie is accepted, and
separately whether restricted entries are actually visible — being signed in
is not the same as having adult content enabled on the account. Once a session
is configured, **Re-read entries that were withheld earlier** goes back over
the items that were linked without tags.

Requests then carry your identity, so the low rate matters more, not less.
