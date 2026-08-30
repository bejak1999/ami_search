"""Would reading only the first pages of "regtimed" find everything new?

The crawler currently sweeps all 211 pages of the pre-owned slice every hour.
If new listings reliably appear at the front of the "regtimed" ordering then a
20-page head pass would do the same job for a twentieth of the requests. Two
earlier attempts to settle this failed for the same reason: they sampled for
minutes, at times when the shop was not listing anything, and read a stable
front as proof that nothing arrives there.

This settles it by measuring what actually arrived and then asking whether the
head would have seen it:

  * a complete enumeration of the slice at the start, and another at the end -
    203 pages each, so the exact set of listings before and after is known
  * a snapshot of the first 20 pages every hour in between

Anything present at the end but not at the start is an arrival. Anything whose
price range moved between the two is a restock. For each of those, the hourly
snapshots say whether a head pass would have caught it, and on which page. If
most arrivals show up on the first pages, the head pass is enough and the
crawler can stop reading 211 pages an hour. If they are scattered, it cannot.

The ordering under test is an argument, because the answer turned out to
depend on it: "regtimed" scored 9 of 592 while "preowned", asked the same
question afterwards, held roughly half the arrivals in a third of the pages.
The head size is an argument too, so a candidate can be tried at the depth it
would actually be crawled at.

    .probe\\preowned-watch.bat           the ordering now in use, 30 pages
    .probe\\regtime-watch.bat            the old one, 20 pages, for comparison

    python .probe/regtime_watch.py watch preowned 30
    python .probe/regtime_watch.py report preowned

State is written to disk after every step, so stopping the machine or closing
the window loses at most the hour in progress. Running "watch" again resumes
rather than starting over.
"""
from __future__ import annotations

import json
import os
import pathlib
import random
import sys
import time
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
os.environ.setdefault("SECRET_KEY", "regtime-watch-not-a-real-secret")

from app.providers.amiami import API_ROOT, AmiAmiProvider  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent


def state_path() -> pathlib.Path:
    """One file per ordering, so two runs cannot overwrite each other."""
    return HERE / f"watch-{SORT_KEY}.json"

#: Defaults, overridden by the command line. The head is what a crawler
#: would re-read each cycle; the ordering is which one it would read.
SORT_KEY = "preowned"
HEAD_PAGES = 30
HOURS = 24
#: Deliberately gentle. The application is very likely crawling from the same
#: address at the same time, and this is a measurement, not a race.
REQUESTS_PER_MINUTE = 6.0
PER_PAGE = 50

provider = AmiAmiProvider()


def pace() -> None:
    """Wait between requests, irregularly, like the crawler does."""
    base = 60.0 / REQUESTS_PER_MINUTE
    time.sleep(random.uniform(base * 0.6, base * 1.6))


def fetch(page: int) -> tuple[list[dict], int]:
    payload = provider._decode(
        provider.request(
            "GET",
            API_ROOT + "/items",
            params={
                "pagemax": PER_PAGE,
                "pagecnt": page,
                "s_cate_tag": 1,
                "s_st_condition_flg": 1,
                "s_sortkey": SORT_KEY,
                "lang": "eng",
            },
        )
    )
    total = int((payload.get("search_result") or {}).get("total_results") or 0)
    return payload.get("items") or [], total


def digest(raw: dict) -> list:
    """What is worth remembering about a listing, kept small.

    The price range is here because it is the only free signal that a shelf
    moved: a used copy arriving or selling usually shifts the cheapest or the
    dearest of them, and the list response gives both without a second call.
    """
    return [raw.get("min_price"), raw.get("max_price")]


def load() -> dict:
    path = state_path()
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    # The first run of this script wrote one file under a fixed name, before
    # the ordering was a variable. Read it rather than discarding 24 hours of
    # measurement.
    legacy = HERE / "regtime-watch.json"
    if SORT_KEY == "regtimed" and legacy.exists():
        return json.loads(legacy.read_text(encoding="utf-8"))
    return {"full": {}, "heads": []}


def save(state: dict) -> None:
    state_path().write_text(json.dumps(state), encoding="utf-8")


def enumerate_all(label: str) -> dict:
    """Every listing in the slice, with its price range."""
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
    return {
        "at": datetime.now(timezone.utc).isoformat(),
        "listings": listings,
    }


def snapshot_head() -> dict:
    """The first pages, with the position each listing sits at."""
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

    try:
        while datetime.now(timezone.utc) < deadline:
            head = snapshot_head()
            state["heads"].append(head)
            save(state)
            left = deadline - datetime.now(timezone.utc)
            print(
                f"  {head['at'][11:16]} UTC  head snapshot {len(state['heads'])}: "
                f"{len(head['positions'])} listings, {left.total_seconds() / 3600:.1f} h to go"
            )
            # Sleep to the next hour, in short steps so Ctrl+C lands promptly.
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
        print("Nothing recorded yet. Run the watch first.")
        return
    if not end:
        print(f"Start recorded at {start['at']}, {len(heads)} hourly snapshot(s) taken.")
        print("The closing enumeration has not run, so there is nothing to compare against.")
        return

    before, after = start["listings"], end["listings"]
    hours = (
        datetime.fromisoformat(end["at"]) - datetime.fromisoformat(start["at"])
    ).total_seconds() / 3600

    arrived = {code for code in after if code not in before}
    departed = {code for code in before if code not in after}
    moved = {
        code
        for code in after
        if code in before and after[code] != before[code]
    }

    print(f"Ordering: {SORT_KEY!r}, first {HEAD_PAGES} pages")
    print(f"Over {hours:.1f} hours, with {len(heads)} snapshot(s) of that head")
    print(f"  {len(before):,} listings at the start, {len(after):,} at the end")
    print(f"  {len(arrived):,} arrived, {len(departed):,} disappeared, "
          f"{len(moved):,} changed price range")
    print()

    # Where a listing was first seen in the head, if it was seen at all.
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
        # What the same number of slots would have caught at random, so the
        # share can be read against something rather than admired on its own.
        slots = HEAD_PAGES * PER_PAGE
        expected = len(group) * min(1.0, slots / max(1, len(after)))
        factor = (len(caught) / expected) if expected else 0
        print(f"{label}: {len(caught):,} of {len(group):,} appeared in the head ({share:.0f}%)")
        print(f"    that is {factor:.2f}x what {slots:,} random slots would have caught")
        if caught:
            positions = sorted(caught.values())
            for pages in (1, 2, 3, 5, 10, 20, 30, 40):
                if pages > HEAD_PAGES:
                    break
                within = sum(1 for p in positions if p <= pages * PER_PAGE)
                print(
                    f"    within the first {pages:>2} page(s): "
                    f"{within:>5,} of {len(group):,} ({within / len(group) * 100:.0f}%)"
                )
        print()

    print("Reading it")
    print("  Above 1.00x the ordering is doing something; at or below it, the")
    print("  head is no better than reading any other part of the slice, and")
    print("  the full sweep is the only thing finding anything.")
    print()
    print("  One caveat this cannot escape: an arrival that appeared and sold out")
    print("  between two snapshots is invisible to both the head and the closing")
    print("  enumeration, so it counts in neither column.")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if len(sys.argv) > 2:
        SORT_KEY = sys.argv[2]
    if len(sys.argv) > 3:
        HEAD_PAGES = int(sys.argv[3])

    if action == "report":
        report()
    elif action == "reset":
        state_path().unlink(missing_ok=True)
        print(f"Cleared {state_path().name}. The next watch starts fresh.")
    else:
        watch()
