# Probes

Investigation tools, not part of the application. Nothing here is imported by
the backend or shipped in the image; they exist so a question about the shop
can be answered by measurement instead of assumption.

Both use the repository's own virtual environment, because AmiAmi sits behind
Cloudflare and refuses requests whose TLS fingerprint looks like a stock HTTP
client. The provider in `backend/app/providers/amiami.py` handles that; a
plain `requests` call or a browser `fetch` cannot.

## `sort_lab.py` — what the orderings actually do

    .probe\sort-lab.bat          double-click it, or

    python .probe/sort_lab.py    then open http://localhost:8710

The batch file works out the paths from where it sits, so it can be run from
anywhere or pinned to a taskbar. If the virtual environment is missing it says
so and how to make one, rather than closing before you can read anything.

Shows any two orderings side by side, marks where a listing sits in the other
one, and can open each product to read how many copies it holds and its intake
counter.

The orderings were found by trying candidate keys and grouping the responses
by the order they came back in. Seven produce something distinct:

| `s_sortkey`    | What it does                          |
| -------------- | ------------------------------------- |
| *(none)*       | The shop's default order              |
| `preowned`     | Pre-owned listings first              |
| `regtimed`     | Product registration, newest first    |
| `regtimea`     | Product registration, oldest first    |
| `releasedated` | Release date, newest first            |
| `releasedatea` | Release date, oldest first            |
| `priced`       | Roughly price-descending, see below   |
| `pricea`       | Price, low to high                    |

The shop's own dropdown offers four of these under labels that do not say what
they do - `regtimed` is "Recently Updated Items", `preowned` is "Updated
Items", and neither name distinguishes them. The lab names them for their
behaviour and keeps the shop's wording in the note, so an entry can still be
matched to the site.

### Which ordering finds a listing that just arrived

Both update orderings do, and the default does not. Measured across the first
twelve products of each, by where the copies a product holds sit in its own
intake counter - a shop that has just taken copies in will be holding the last
numbers it issued, consecutively:

| ordering   | copies held | spread in the counter | gaps | consecutive at the top |
| ---------- | ----------- | --------------------- | ---- | ---------------------- |
| default    | 4.2         | 26                    | 21   | 3 of 11                |
| `regtimed` | 2.4         | 2                     | 0    | 11 of 12               |
| `preowned` | 2.4         | 2                     | 0    | 7 of 12                |

So the front of `regtimed` is almost entirely products the shop has just
restocked, `preowned` mostly so, and the default order is neither - its front
holds shelves whose copies were taken in at scattered times.

This is a snapshot correlation, not proof; `order_probe.py` records the same
orderings over time to confirm it.

The catch that hid most of these: a key without its direction suffix is
accepted and then ignored. Asking for `price` returns the default order, which
looks exactly like "the API does not support sorting by price". Only `pricea`
and `priced` do anything. An earlier probe tested the bare names and concluded
the API had two orderings; it has eight.

Worth knowing: the sort is applied by the shop across the whole result set,
not to the page being fetched. `pricea` on ten thousand pre-owned figures
really does start at 70 yen.

`priced` is the odd one out. It returns expensive listings, but not in order -
129,980 then 188,280 then 160,650 - and the sequence matches neither the
asking price nor the list price the response carries. It is a distinct
ordering, just not the clean mirror of `pricea` its name suggests, so the page
labels it as approximate rather than pretending otherwise.

## `regtime_watch.py` — would reading only the first pages be enough?

    .probe
egtime-watch.bat          start it, leave it, Ctrl+C to stop
    python .probe/regtime_watch.py report

The crawler sweeps all 211 pages of the pre-owned slice every hour. If new
listings reliably appear at the front of the `regtimed` ordering, a 20-page
head pass would do the same job for a twentieth of the requests.

Two earlier attempts to settle this failed the same way: they sampled for
minutes, at times when the shop was not listing anything, and read a stable
front as proof that nothing arrives there. This one measures what actually
arrived and then asks whether the head would have seen it:

* a complete enumeration of the slice at the start and another at the end, so
  the exact set of listings before and after is known
* a 20-page snapshot every hour in between

Anything present at the end but not at the start is an arrival; anything whose
price range moved is a restock. For each, the hourly snapshots say whether a
head pass would have caught it and on which page. Run it for a full day so it
covers Japanese business hours, which is when listings are actually made.

It costs about 900 requests over 24 hours, paced at six a minute, and saves
after every step so an interruption loses at most the hour in progress.

One thing it cannot see: a listing that appeared and sold out between two
snapshots is invisible to both the head and the closing enumeration, so it
counts in neither column.

### The answer, measured 28-29 August 2026

Over 24.7 hours covering a full Japanese working day, 23 hourly snapshots:

    10,514 listings at the start, 10,597 at the end
    592 arrived, 509 disappeared, 293 changed price range

    Arrivals:            9 of 592 appeared in the head   (2%)
      first  2 pages:    0
      first 10 pages:    7
    Price-range changes: 55 of 293 appeared in the head  (19%)

Twenty pages are 9.5% of the slice, so an even spread would have put 56 of
the 592 arrivals there. Nine turned up. New listings are about six times
*less* likely to be at the front than under a shuffle, so `regtimed` does not
merely fail to help - it pushes them away from it. Price-range changes do come
out about twice as often as chance, which is a real but useless signal: four
fifths would still be missed.

So the head-pass idea is dead in both orderings the API offers, and the
crawler keeps sweeping each slice end to end.

The same run gave the number that sets the pace: 24 listings arrive and 21
disappear every hour, so an hourly full sweep of the pre-owned slice catches
every arrival within the hour.

## `order_probe.py` — does a listing move when it is restocked?

    python .probe/order_probe.py record      # baseline
    ... hours later ...
    python .probe/order_probe.py record
    python .probe/order_probe.py compare

Answers whether the front pages of a slice are where new listings appear,
which decides whether re-reading only those pages is worth anything. The
findings, and the caveat that they are only as current as the last
comparison, are at the top of the script.
