"""Does a pre-owned listing move to the front when a new copy arrives?

The crawler used to re-read only the first pages of a slice, which is worth
doing only if the shop puts recently-listed or recently-restocked products
there. Nothing in the list payload says when a used listing was created -
there is no date field at all beyond the figure's original release - so the
only way to find out is to watch the same pages over time.

    python .probe/order_probe.py record      # baseline
    ... hours later ...
    python .probe/order_probe.py record      # again
    python .probe/order_probe.py compare

Answered, over a nine-hour window on 28 August 2026:

    375 new pre-owned listings appeared. None on page one; the only arrival
    there had drifted up from page two because an entry above it left.

    FIGURE-184067-R took in five copies (counter 120 -> 125) and did not move
    at all, staying at slot two of page one.

    Of 49 listings still on page one, zero had changed rank relative to each
    other. The order is fixed; pages drift only because entries above them
    disappear, which is why page 50 kept two thirds of its content while
    pages 100 and 150 kept none.

Conclusion: page position says nothing about when a listing was added or last
restocked, so the crawler reads every slice end to end. Worth re-running if
the shop ever changes its ordering - the conclusion is only as current as the
last comparison.
"""
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
os.environ.setdefault("SECRET_KEY", "probe-only-not-a-real-secret")

from app.providers.amiami import AmiAmiProvider, API_ROOT  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
#: The head the crawler used to re-read, plus three deep pages as a control.
PAGES = (1, 2, 3, 50, 100, 150)
#: Products to follow individually, so a move can be tied to a new copy.
SAMPLE_PER_PAGE = 4
#: Which ordering to follow. The shop's dropdown offers "Recently Updated
#: Items" (regtimed) and "Updated Items" (preowned); which of the two actually
#: reacts to a copy arriving is the open question, so both can be recorded.
SORT = (sys.argv[2] if len(sys.argv) > 2 else "")

provider = AmiAmiProvider()


def fetch(page: int) -> list[dict]:
    params = {
        "pagemax": 50,
        "pagecnt": page,
        "s_cate_tag": 1,
        "s_st_condition_flg": 1,
        "lang": "eng",
    }
    if SORT:
        params["s_sortkey"] = SORT
    payload = provider._decode(provider.request("GET", API_ROOT + "/items", params=params))
    return payload.get("items") or []


def shelf(code: str) -> dict:
    """The copies currently listed under one product, by their intake number."""
    from app.services.shelflife import sequence_of

    try:
        detail = provider.get_item(code)
    except Exception as exc:  # noqa: BLE001 - a missing product is a result too
        return {"error": str(exc)[:80]}
    numbers = sorted(
        n for v in detail.variants if (n := sequence_of(v.get("code"))) is not None
    )
    return {"copies": numbers, "highest": numbers[-1] if numbers else None}


def record() -> None:
    snapshot = {
        "taken_at": datetime.now(timezone.utc).isoformat(),
        "sort": SORT,
        "pages": {},
        "shelves": {},
    }
    for page in PAGES:
        items = fetch(page)
        codes = [i.get("gcode") for i in items]
        snapshot["pages"][str(page)] = codes
        for code in codes[:SAMPLE_PER_PAGE]:
            if code:
                snapshot["shelves"][code] = shelf(code)
        print(f"  page {page:>3}: {len(codes)} listings, {SAMPLE_PER_PAGE} shelves read")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = HERE / f"order-{SORT or 'default'}-{stamp}.json"
    path.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    print(f"\nBaseline written to {path.name}")


def compare() -> None:
    files = sorted(HERE.glob(f"order-{SORT or 'default'}-*.json"))
    if len(files) < 2:
        print("Need two snapshots. Run 'record' again later.")
        return
    old = json.loads(files[0].read_text(encoding="utf-8"))
    new = json.loads(files[-1].read_text(encoding="utf-8"))
    print(f"Ordering: {SORT or '(default)'}")
    print(f"Comparing {files[0].name} -> {files[-1].name}")
    print(f"  {old['taken_at']}  ->  {new['taken_at']}\n")

    where_old = {
        code: int(page)
        for page, codes in old["pages"].items()
        for code in codes
        if code
    }
    moved_up, unchanged = [], 0
    for page, codes in new["pages"].items():
        for code in codes:
            was = where_old.get(code)
            if was is None:
                continue
            if was == int(page):
                unchanged += 1
            elif int(page) < was:
                moved_up.append((code, was, int(page)))

    print("Page composition")
    for page in PAGES:
        a = old["pages"].get(str(page), [])
        b = new["pages"].get(str(page), [])
        same = sum(1 for x, y in zip(a, b) if x == y)
        fresh = len(set(b) - set(a))
        print(f"  page {page:>3}: {same}/{len(b)} in the same slot, {fresh} not there before")

    print(f"\n{unchanged} listing(s) stayed on their page, {len(moved_up)} moved forward")

    print("\nDid a product that moved forward gain a copy?")
    if not moved_up:
        print("  nothing moved between the sampled pages")
    for code, was, now in moved_up[:10]:
        before = old["shelves"].get(code, {}).get("highest")
        after = new["shelves"].get(code)
        after = after.get("highest") if after else shelf(code).get("highest")
        verdict = (
            "yes, a new copy" if (before and after and after > before)
            else "no change in its copies" if before and after
            else "not sampled"
        )
        print(f"  {code:<24} page {was} -> {now}   highest copy {before} -> {after}   {verdict}")

    print("\nShelves of the products followed individually")
    for code, entry in list(new["shelves"].items())[:12]:
        before = old["shelves"].get(code, {}).get("highest")
        after = entry.get("highest")
        if before is None and after is None:
            continue
        mark = "  <-- restocked" if before and after and after > before else ""
        print(f"  {code:<24} highest copy {before} -> {after}{mark}")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "record"
    (record if action == "record" else compare)()
