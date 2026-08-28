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

## `order_probe.py` — does a listing move when it is restocked?

    python .probe/order_probe.py record      # baseline
    ... hours later ...
    python .probe/order_probe.py record
    python .probe/order_probe.py compare

Answers whether the front pages of a slice are where new listings appear,
which decides whether re-reading only those pages is worth anything. The
findings, and the caveat that they are only as current as the last
comparison, are at the top of the script.
