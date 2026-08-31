"""Does the "preowned" ordering keep newly taken-in copies near the front?

Standalone on purpose. Nothing here imports the application, so the folder it
lives in can be copied to a memory stick and run on any Windows machine with
Python on it - the point being that a laptop can carry the measurement instead
of leaving a desktop switched on for a day.

The question it answers
-----------------------
AmiAmi sells a used figure as separate graded copies, and takes a copy's
listing down when it sells. The crawler wants to notice a new copy quickly
without re-reading all 213 pages of the pre-owned catalogue every hour. That
only works if the ordering it reads carries new arrivals towards the front.

Measured for "regtimed" over 24.7 hours, it does not: 9 of 592 arrivals ever
appeared in the first twenty pages, which is worse than picking at random.
"preowned" - the shop's own 中古 ordering - looked far better in a one-off
comparison, holding 300 of those same 594 in twenty pages. This is the
longitudinal check on that: enumerate the whole slice at both ends so the
arrivals are known exactly, snapshot the head every hour in between, and see
how many of them the head would actually have caught.

How it runs
-----------
  * a full enumeration at the start - 213 pages, about half an hour
  * a snapshot of the first 30 pages every hour for 24 hours
  * a full enumeration at the end, and then the report

State is written after every step, so closing the window or losing power costs
at most the hour in progress. Starting it again resumes rather than restarting.
Deliberately gentle: six requests a minute, spaced irregularly.
"""
from __future__ import annotations

import json
import pathlib
import random
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - the launcher installs it
    print("curl_cffi is missing. Run start.bat, which installs it into lib\\.")
    raise SystemExit(1)

sys.stdout.reconfigure(encoding="utf-8")

API_ROOT = "https://api.amiami.com/api/v1.0"
SITE_ROOT = "https://www.amiami.com"

#: The shop refuses a plain HTTP client on its TLS fingerprint, so the session
#: impersonates a browser. This is the same handshake the application uses.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-User-Key": "amiami_dev",
    "Origin": SITE_ROOT,
    "Referer": SITE_ROOT + "/",
}

SORT_KEY = "preowned"
HEAD_PAGES = 30
HOURS = 24
PER_PAGE = 50
REQUESTS_PER_MINUTE = 6.0

STATE = HERE / "measurement.json"
session = curl_requests.Session(headers=HEADERS, impersonate="chrome", timeout=30)


def pace() -> None:
    """Wait between requests, irregularly, the way the application does."""
    base = 60.0 / REQUESTS_PER_MINUTE
    time.sleep(random.uniform(base * 0.6, base * 1.6))


def fetch(page: int) -> tuple[list[dict], int]:
    params = {
        "pagemax": PER_PAGE,
        "pagecnt": page,
        "s_cate_tag": 1,
        "s_st_condition_flg": 1,
        "s_sortkey": SORT_KEY,
        "lang": "eng",
    }
    for attempt in range(4):
        try:
            response = session.get(API_ROOT + "/items", params=params)
            response.raise_for_status()
            payload = response.json()
            total = int((payload.get("search_result") or {}).get("total_results") or 0)
            return payload.get("items") or [], total
        except Exception as exc:  # noqa: BLE001 - a laptop's network comes and goes
            if attempt == 3:
                raise
            wait = 20 * (attempt + 1)
            print(f"    request failed ({exc}); retrying in {wait}s")
            time.sleep(wait)
    return [], 0


def digest(raw: dict) -> list:
    """What is worth remembering, kept small.

    The price range is here because it is the only free signal that a shelf
    moved: a copy arriving or selling usually shifts the cheapest or the
    dearest, and the list response gives both without a second call.
    """
    return [raw.get("min_price"), raw.get("max_price")]


def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"full": {}, "heads": []}


def save(state: dict) -> None:
    # Written beside the target and moved, so pulling the stick out mid-write
    # cannot leave a half-written file that later fails to parse.
    staging = STATE.with_suffix(".part")
    staging.write_text(json.dumps(state), encoding="utf-8")
    staging.replace(STATE)


def enumerate_all(label: str) -> dict:
    print(f"  Full enumeration ({label}) - this takes about half an hour")
    listings: dict[str, list] = {}
    page = 1
    pages_total = None
    while True:
        items, total = fetch(page)
        if pages_total is None and total:
            pages_total = -(-total // PER_PAGE)
            print(f"    {total:,} listings over {pages_total} pages")
        if not items:
            break
        for raw in items:
            code = raw.get("gcode")
            if code:
                listings[code] = digest(raw)
        if page % 25 == 0 or page == pages_total:
            print(f"    page {page} of {pages_total}, {len(listings):,} so far")
        if pages_total and page >= pages_total:
            break
        page += 1
        pace()
    return {"at": datetime.now(timezone.utc).isoformat(), "listings": listings}


def snapshot_head() -> dict:
    positions: dict[str, int] = {}
    ranges: dict[str, list] = {}
    for page in range(1, HEAD_PAGES + 1):
        items, _ = fetch(page)
        if not items:
            break
        for offset, raw in enumerate(items):
            code = raw.get("gcode")
            if code and code not in positions:
                positions[code] = (page - 1) * PER_PAGE + offset + 1
                ranges[code] = digest(raw)
        if page < HEAD_PAGES:
            pace()
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "positions": positions,
        "ranges": ranges,
    }


def watch() -> None:
    state = load()

    if not state["full"].get("start"):
        state["full"]["start"] = enumerate_all("start")
        save(state)
        print(f"  recorded {len(state['full']['start']['listings']):,} listings\n")
    else:
        print(f"  Resuming: start already recorded at {state['full']['start']['at']}")
        print(f"  {len(state['heads'])} hourly snapshot(s) taken so far\n")

    started = datetime.fromisoformat(state["full"]["start"]["at"])
    deadline = started + timedelta(hours=HOURS)

    if datetime.now(timezone.utc) >= deadline and not state["heads"]:
        print("  The window has already passed and nothing was sampled in it.")
        print("  Run reset.bat and start again.\n")
        return

    try:
        while datetime.now(timezone.utc) < deadline:
            head = snapshot_head()
            state["heads"].append(head)
            save(state)
            left = deadline - datetime.now(timezone.utc)
            print(
                f"  {head['at'][11:16]} UTC  snapshot {len(state['heads'])}: "
                f"{len(head['positions'])} listings, {left.total_seconds() / 3600:.1f} h to go"
            )
            # Slept in short steps so Ctrl+C lands promptly and a laptop
            # waking from sleep notices the deadline rather than overshooting.
            for _ in range(360):
                if datetime.now(timezone.utc) >= deadline:
                    break
                time.sleep(10)
    except KeyboardInterrupt:
        print("\n  Stopped early. What has been collected is kept.")

    print()
    state["full"]["end"] = enumerate_all("end")
    save(state)
    print(f"  recorded {len(state['full']['end']['listings']):,} listings\n")
    report()


def report() -> None:
    state = load()
    start = state["full"].get("start")
    end = state["full"].get("end")
    heads = state.get("heads") or []

    if not start:
        print("Nothing recorded yet. Run start.bat first.")
        return
    if not end:
        print(f"Start recorded at {start['at']}, {len(heads)} snapshot(s) taken.")
        print("The closing enumeration has not run, so there is nothing to compare against.")
        print("Run start.bat again - it resumes, and finishes with the closing pass.")
        return

    before, after = start["listings"], end["listings"]
    hours = (
        datetime.fromisoformat(end["at"]) - datetime.fromisoformat(start["at"])
    ).total_seconds() / 3600

    arrived = {code for code in after if code not in before}
    departed = {code for code in before if code not in after}
    moved = {c for c in after if c in before and after[c] != before[c]}

    print(f"Ordering: {SORT_KEY!r}, first {HEAD_PAGES} pages")
    print(f"Over {hours:.1f} hours, with {len(heads)} snapshot(s) of that head")
    print(f"  {len(before):,} listings at the start, {len(after):,} at the end")
    print(f"  {len(arrived):,} arrived, {len(departed):,} disappeared, "
          f"{len(moved):,} changed price range")

    # An unwatched stretch flatters nothing and biases everything: an arrival
    # nobody was looking for cannot have been caught, so it counts against the
    # head through no fault of the ordering.
    if heads:
        watched = (
            datetime.fromisoformat(heads[-1]["at"])
            - datetime.fromisoformat(heads[0]["at"])
        ).total_seconds() / 3600
        if hours - watched > 2:
            print()
            print(f"  Careful: the head was only sampled across {watched:.1f} of those "
                  f"{hours:.1f} hours.")
            print("  Arrivals in the unwatched stretch had no chance of being seen, so the")
            print("  shares below understate what the head would really catch.")
    print()

    first_seen: dict[str, int] = {}
    for head in heads:
        for code, position in head["positions"].items():
            first_seen.setdefault(code, position)

    for label, group in (("Arrivals", arrived), ("Price-range changes", moved)):
        if not group:
            print(f"{label}: none")
            continue
        caught = {code: first_seen[code] for code in group if code in first_seen}
        share = len(caught) / len(group) * 100
        slots = HEAD_PAGES * PER_PAGE
        expected = len(group) * min(1.0, slots / max(1, len(after)))
        factor = (len(caught) / expected) if expected else 0
        print(f"{label}: {len(caught):,} of {len(group):,} appeared in the head ({share:.0f}%)")
        print(f"    that is {factor:.2f}x what {slots:,} random slots would have caught")
        if caught:
            positions = sorted(caught.values())
            for pages in (1, 2, 5, 10, 20, 30):
                if pages > HEAD_PAGES:
                    break
                within = sum(1 for p in positions if p <= pages * PER_PAGE)
                print(f"    within the first {pages:>2} page(s): "
                      f"{within:>5,} of {len(group):,} ({within / len(group) * 100:.0f}%)")
        print()

    print("Reading it")
    print("  Above 1.00x the ordering is doing something; at or below it, the head is")
    print("  no better than reading any other part of the slice, and only the full")
    print("  sweep is finding anything.")
    print()
    print("  One caveat nothing can escape: a copy that appeared and sold between two")
    print("  snapshots is invisible to the head and to the closing enumeration alike,")
    print("  so it counts in neither column.")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if action == "report":
        report()
    elif action == "reset":
        STATE.unlink(missing_ok=True)
        print("Cleared. The next start begins a fresh measurement.")
    else:
        watch()
