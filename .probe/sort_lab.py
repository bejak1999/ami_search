"""A small viewer for AmiAmi's list orderings.

    python .probe/sort_lab.py          then open http://localhost:8710

Two things make a plain HTML file insufficient here, which is why this serves
its own page instead. AmiAmi sits behind Cloudflare and refuses a request
whose TLS fingerprint looks like a stock HTTP client, so the browser could not
call it directly even without the cross-origin block; the request has to go
through the same provider the application uses. And the responses are worth
enriching - the number of copies a product currently holds, and its intake
counter - which needs a second call per row.

What the orderings are was measured rather than guessed. The bare names the
site suggests (regtime, price, releasedate) are silently ignored; only the
ones carrying a direction suffix change anything. Everything confirmed to
produce a distinct order is in ORDERINGS below, and the page lets you type a
key of your own to look for more.
"""
from __future__ import annotations

import html
import json
import pathlib
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
import os  # noqa: E402

os.environ.setdefault("SECRET_KEY", "sort-lab-not-a-real-secret-value")

from app.providers.amiami import API_ROOT, AmiAmiProvider  # noqa: E402
from app.services.shelflife import sequence_of  # noqa: E402

PORT = 8710
provider = AmiAmiProvider()

#: Every ordering confirmed to return something different from the default.
#: A name without its direction suffix is accepted by the API and then
#: ignored, which is exactly how this was missed the first time round.
ORDERINGS = [
    ("", "Default (no sort key)", "What the shop returns when asked for nothing."),
    ("preowned", "Pre-owned first", "The only key that works without a direction."),
    ("regtimed", "Registered, newest first", "When the product entry was created."),
    ("regtimea", "Registered, oldest first", "The same axis, reversed."),
    ("releasedated", "Release date, newest first", "The figure's own release, not the listing's."),
    ("releasedatea", "Release date, oldest first", ""),
    ("priced", "Price, high to low", "Sorted by the shop across the whole result."),
    ("pricea", "Price, low to high", ""),
]

CONDITIONS = [
    ("any", "Everything"),
    ("preowned", "Pre-owned only"),
    ("instock", "First-hand stock"),
    ("preorder", "Pre-orderable"),
]


def build_params(query: dict) -> dict:
    params = {
        "pagemax": 20,
        "pagecnt": max(1, int(query.get("page", ["1"])[0] or 1)),
        "lang": "eng",
    }
    if query.get("category", ["1"])[0]:
        params["s_cate_tag"] = int(query["category"][0])
    condition = query.get("condition", ["any"])[0]
    if condition == "preowned":
        params["s_st_condition_flg"] = 1
    elif condition == "instock":
        params["s_st_list_newitem_available"] = 1
    elif condition == "preorder":
        params["s_st_list_preorder_available"] = 1
    keywords = (query.get("q", [""])[0] or "").strip()
    if keywords:
        params["s_keywords"] = keywords
    sort = (query.get("sort", [""])[0] or "").strip()
    if sort:
        params["s_sortkey"] = sort
    return params


def listing(query: dict) -> dict:
    params = build_params(query)
    payload = provider._decode(provider.request("GET", API_ROOT + "/items", params=params))
    items = payload.get("items") or []
    rows = []
    for position, raw in enumerate(items, start=1):
        code = raw.get("gcode") or ""
        thumb = raw.get("thumb_url") or ""
        rows.append(
            {
                "position": position,
                "code": code,
                "name": raw.get("gname") or "",
                "maker": raw.get("maker_name") or "",
                "min_price": raw.get("min_price"),
                "max_price": raw.get("max_price"),
                "release": (raw.get("releasedate") or "")[:10],
                "in_stock": bool(raw.get("instock_flg")),
                "preowned": bool(raw.get("condition_flg")),
                "thumb": ("https://img.amiami.com" + thumb) if thumb.startswith("/") else thumb,
                "url": provider.product_url(code),
            }
        )
    return {
        "total": (payload.get("search_result") or {}).get("total_results"),
        "params": {k: str(v) for k, v in params.items()},
        "items": rows,
    }


def shelf(code: str) -> dict:
    """Copies on the shelf and the product's intake counter.

    A used product's copies are numbered per product in the order the shop
    takes them in, so the highest number is how many it has ever handled and
    a low one means the listing itself is young. That is the single most
    useful thing to know when judging what an ordering is actually sorted by.
    """
    try:
        detail = provider.get_item(code)
    except Exception as exc:  # noqa: BLE001 - a deleted listing is an answer
        return {"code": code, "error": str(exc)[:120]}
    numbers = sorted(n for v in detail.variants if (n := sequence_of(v.get("code"))) is not None)
    return {
        "code": code,
        "copies": len(detail.variants),
        "highest": numbers[-1] if numbers else None,
        "lowest": numbers[0] if numbers else None,
    }


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AmiAmi sort lab</title>
<style>
:root{color-scheme:dark light;--bg:#12151b;--card:#1a1f27;--line:#2a313c;--ink:#e6eaf0;
--muted:#98a3b2;--faint:#6d7889;--accent:#f2802f;--ok:#37c7b7}
@media(prefers-color-scheme:light){:root{--bg:#eef0f3;--card:#fff;--line:#d5dae1;
--ink:#141922;--muted:#5b6675;--faint:#8a94a3;--accent:#b4400c;--ok:#0e6e66}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:1.2rem 1.5rem;border-bottom:1px solid var(--line)}
h1{margin:0 0 .3rem;font-size:1.2rem}
p.lead{margin:0;color:var(--muted);font-size:.85rem;max-width:70ch}
form{display:flex;flex-wrap:wrap;gap:.6rem;align-items:end;padding:1rem 1.5rem;
border-bottom:1px solid var(--line)}
label{display:flex;flex-direction:column;gap:.25rem;font-size:.72rem;
text-transform:uppercase;letter-spacing:.07em;color:var(--faint)}
select,input,button{background:var(--card);color:var(--ink);border:1px solid var(--line);
border-radius:6px;padding:.45rem .6rem;font:inherit;font-size:.85rem}
button{cursor:pointer;border-color:var(--accent);color:var(--accent)}
button:hover{background:var(--accent);color:var(--bg)}
.hint{color:var(--faint);font-size:.75rem;padding:0 1.5rem 1rem;max-width:80ch}
.panes{display:grid;gap:1rem;padding:1.5rem;grid-template-columns:1fr}
.panes.two{grid-template-columns:1fr 1fr}
@media(max-width:900px){.panes.two{grid-template-columns:1fr}}
.pane{background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
.pane h2{margin:0;padding:.7rem 1rem;font-size:.9rem;border-bottom:1px solid var(--line);
display:flex;justify-content:space-between;gap:1rem;align-items:baseline}
.pane h2 small{color:var(--faint);font-weight:400;font-size:.75rem}
.row{display:grid;grid-template-columns:2rem 44px 1fr auto;gap:.7rem;align-items:center;
padding:.5rem 1rem;border-bottom:1px solid var(--line);font-size:.82rem}
.row:last-child{border-bottom:0}
.row img{width:44px;height:44px;object-fit:cover;border-radius:4px;background:var(--bg)}
.pos{color:var(--faint);font-variant-numeric:tabular-nums;text-align:right}
.nm{min-width:0}
.nm a{color:inherit;text-decoration:none;display:block;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap}
.nm a:hover{color:var(--accent)}
.meta{color:var(--faint);font-size:.72rem;font-variant-numeric:tabular-nums}
.px{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.shelf{color:var(--ok);font-size:.72rem;font-variant-numeric:tabular-nums}
.moved{color:var(--accent)}
.err{padding:1rem;color:var(--accent);font-size:.85rem}
code{background:var(--bg);padding:.1em .35em;border-radius:3px;font-size:.9em}
</style></head><body>
<header>
  <h1>AmiAmi sort lab</h1>
  <p class="lead">The orderings the API actually distinguishes. A key without its
  direction suffix &mdash; <code>price</code> rather than <code>pricea</code> &mdash; is accepted
  and then ignored, so it comes back as the default order.</p>
</header>
<form id="f" onsubmit="run(event)">
  <label>Ordering<select id="sort">__ORDERINGS__</select></label>
  <label>Compare with<select id="sort2"><option value="__none__">nothing</option>__ORDERINGS__</select></label>
  <label>Filter<select id="condition">__CONDITIONS__</select></label>
  <label>Keywords<input id="q" placeholder="optional" size="14"></label>
  <label>Category<input id="category" value="1" size="4" title="1 = figures"></label>
  <label>Page<input id="page" type="number" value="1" min="1" size="4"></label>
  <button type="submit">Load</button>
  <button type="button" onclick="enrich()">Read shelves</button>
</form>
<p class="hint">&ldquo;Read shelves&rdquo; opens each product to count the copies it holds and
read its intake number &mdash; the highest is how many used copies the shop has ever handled,
so a low one means the listing itself is young. Slow on purpose: one request per row.</p>
<div class="panes" id="panes"></div>
<script>
const $ = id => document.getElementById(id)
let last = {}

function money(v){ return v == null ? '' : '\\u00a5' + Number(v).toLocaleString('en-GB') }

function render(pane, data, other){
  if (data.error) return `<div class="err">${data.error}</div>`
  const where = new Map((other?.items || []).map(r => [r.code, r.position]))
  return data.items.map(r => {
    const was = where.get(r.code)
    const shift = was == null ? '' :
      was === r.position ? '<span class="meta">same slot</span>' :
      `<span class="meta moved">was #${was}</span>`
    const shelf = last.shelves?.[r.code]
    const shelfText = !shelf ? '' : shelf.error ? '<span class="meta">gone</span>' :
      `<span class="shelf">${shelf.copies} copies &middot; #${shelf.highest}</span>`
    return `<div class="row">
      <span class="pos">${r.position}</span>
      <img loading="lazy" src="${r.thumb}" alt="">
      <span class="nm">
        <a href="${r.url}" target="_blank" rel="noreferrer">${r.name}</a>
        <span class="meta">${r.code} &middot; ${r.maker} &middot; ${r.release}
          ${r.preowned ? '&middot; pre-owned' : ''} ${shift} ${shelfText}</span>
      </span>
      <span class="px">${money(r.min_price)}${
        r.max_price && r.max_price !== r.min_price ? '<br><span class="meta">to ' + money(r.max_price) + '</span>' : ''}</span>
    </div>`
  }).join('')
}

function paneHtml(title, data, other){
  const label = data.params ? Object.entries(data.params)
    .filter(([k]) => k.startsWith('s_')).map(([k,v]) => k + '=' + v).join(' ') || 'no filters' : ''
  return `<section class="pane"><h2>${title}
    <small>${data.total ? data.total.toLocaleString('en-GB') + ' results &middot; ' : ''}${label}</small>
  </h2>${render(null, data, other)}</section>`
}

async function fetchList(sort){
  const p = new URLSearchParams({
    sort, condition: $('condition').value, q: $('q').value,
    category: $('category').value, page: $('page').value,
  })
  const res = await fetch('/api/list?' + p)
  if (!res.ok) return { error: 'Request failed: ' + res.status }
  return res.json()
}

async function run(event){
  if (event) event.preventDefault()
  $('panes').innerHTML = '<div class="err">Loading&hellip;</div>'
  const a = await fetchList($('sort').value)
  const second = $('sort2').value
  const b = second === '__none__' ? null : await fetchList(second)
  last = { a, b, shelves: last.shelves || {} }
  const name = id => $(id).selectedOptions[0].textContent
  $('panes').className = 'panes' + (b ? ' two' : '')
  $('panes').innerHTML = paneHtml(name('sort'), a, b) + (b ? paneHtml(name('sort2'), b, a) : '')
}

async function enrich(){
  const codes = [...new Set([...(last.a?.items || []), ...(last.b?.items || [])].map(r => r.code))]
  last.shelves = last.shelves || {}
  for (const code of codes){
    if (last.shelves[code]) continue
    const res = await fetch('/api/shelf?code=' + encodeURIComponent(code))
    last.shelves[code] = await res.json()
    const name = id => $(id).selectedOptions[0].textContent
    $('panes').innerHTML = paneHtml(name('sort'), last.a, last.b)
      + (last.b ? paneHtml(name('sort2'), last.b, last.a) : '')
  }
}

run()
</script></body></html>
"""


def render_page() -> str:
    options = "".join(
        f'<option value="{html.escape(key)}" title="{html.escape(note)}">'
        f"{html.escape(label)}</option>"
        for key, label, note in ORDERINGS
    )
    conditions = "".join(
        f'<option value="{key}">{html.escape(label)}</option>' for key, label in CONDITIONS
    )
    return PAGE.replace("__ORDERINGS__", options).replace("__CONDITIONS__", conditions)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102 - one line per request is noise
        pass

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - the stdlib names it
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path in ("/", "/index.html"):
                self._send(render_page().encode("utf-8"), "text/html; charset=utf-8")
            elif parsed.path == "/api/list":
                self._send(json.dumps(listing(query)).encode("utf-8"), "application/json")
            elif parsed.path == "/api/shelf":
                code = query.get("code", [""])[0]
                self._send(json.dumps(shelf(code)).encode("utf-8"), "application/json")
            else:
                self.send_error(404)
        except Exception as exc:  # noqa: BLE001 - a tool should say what broke
            self._send(
                json.dumps({"error": f"{type(exc).__name__}: {exc}"}).encode("utf-8"),
                "application/json",
            )


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Sort lab on {url}   (ctrl-c to stop)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
