"""Every sort key AmiAmi's own dropdown offers, including the hidden ones.

The Japanese pre-owned page carries this select, two of whose options are
commented out in the markup - so they are not offered to a visitor, but the
API may well still honour them:

    ""              おすすめ順          recommended (the default)
    regtimed        新着順              newest first
    pricea          価格が安い          cheapest first
    priced          価格が高い          dearest first
    releasedated    発売日が新しい      newest release date
    releasedatea    発売日(古い順)      oldest release date      [commented out]
    buy_priced      買取価格(高い順)    highest buyback price    [commented out]
    preowned        中古                used

This asks the API for each of them and reports whether the ordering it gives
back is real or ignored. A key AmiAmi does not recognise is not rejected: it
comes back with the unordered default, which looks like a working sort until
you compare it against one.

    python .probe/sort_options.py
"""
from __future__ import annotations

import os
import pathlib
import random
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
os.environ.setdefault("SECRET_KEY", "sort-options-not-a-real-secret")

from app.providers.amiami import API_ROOT, AmiAmiProvider  # noqa: E402

KEYS = [
    ("", "recommended (default)", "おすすめ順"),
    ("regtimed", "newest first", "新着順"),
    ("pricea", "cheapest first", "価格が安い"),
    ("priced", "dearest first", "価格が高い"),
    ("releasedated", "newest release date", "発売日が新しい"),
    ("releasedatea", "oldest release date", "発売日(古い順) [hidden]"),
    ("buy_priced", "highest buyback price", "買取価格(高い順) [hidden]"),
    ("preowned", "used", "中古"),
    ("regtimea", "oldest first", "(not offered, tried anyway)"),
]

provider = AmiAmiProvider()


def page(sortkey: str, pagecnt: int = 1) -> list[dict]:
    params = {
        "pagemax": 50,
        "pagecnt": pagecnt,
        "s_cate_tag": 1,
        "s_st_condition_flg": 1,
        "lang": "eng",
    }
    if sortkey:
        params["s_sortkey"] = sortkey
    payload = provider._decode(provider.request("GET", API_ROOT + "/items", params=params))
    return payload.get("items") or []


def describe(items: list[dict]) -> str:
    """Whatever ordering signal the first page shows, in one line."""
    prices = [i.get("min_price") for i in items if i.get("min_price")]
    dates = [str(i.get("releasedate") or "")[:7] for i in items if i.get("releasedate")]
    codes = [i.get("gcode", "") for i in items]

    def numeric(code: str) -> int | None:
        parts = code.split("-")
        return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None

    ids = [n for n in (numeric(c) for c in codes) if n]
    bits = []
    if prices:
        bits.append(f"price {prices[0]:,}→{prices[-1]:,}")
    if dates:
        bits.append(f"release {dates[0]}→{dates[-1]}")
    if ids:
        bits.append(f"product id {ids[0]:,}→{ids[-1]:,}")
    return "  ".join(bits)


def main() -> None:
    baseline = [i.get("gcode") for i in page("")]
    print(f"Pre-owned figures, first 50 of each ordering\n")
    print(f"  {'key':<14}{'means':<24}{'vs default':<12}what it looks like")
    print(f"  {'-' * 100}")

    for key, meaning, japanese in KEYS:
        time.sleep(random.uniform(6, 10))
        try:
            items = page(key)
        except Exception as exc:  # noqa: BLE001
            print(f"  {key or '(none)':<14}{meaning:<24}{'error':<12}{exc}")
            continue
        codes = [i.get("gcode") for i in items]
        if not codes:
            print(f"  {key or '(none)':<14}{meaning:<24}{'empty':<12}no results")
            continue
        same = "identical" if codes == baseline else "different"
        print(f"  {key or '(none)':<14}{meaning:<24}{same:<12}{describe(items)}")

    print(
        "\n  'identical' means AmiAmi ignored the key and served the default order.\n"
        "  A key it honours reorders the result set, not just the page."
    )


if __name__ == "__main__":
    main()
