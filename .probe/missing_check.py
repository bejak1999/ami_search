"""Are the arrivals a head pass misses still on sale at all?

Reading 30 pages of the 中古 ordering finds 452 of the 594 known arrivals, and
reading ten pages further finds not one more. A flat plateau like that is not
what a merely imperfect ordering looks like - it is what it looks like when
the rest are no longer there to be found.

So this asks the shop about a sample of the ones the head missed. If they have
sold, then the head is not missing them: nothing could have found them a day
later, and its real recall among listings that still exist is far higher than
76%. If they are still on sale and simply sit deep in the ordering, the head
genuinely misses them and 76% is the honest figure.

    python .probe/missing_check.py
"""
from __future__ import annotations

import json
import os
import pathlib
import random
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
os.environ.setdefault("SECRET_KEY", "missing-check-not-a-real-secret")

from app.providers.amiami import API_ROOT, AmiAmiProvider  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
WATCH = HERE / "regtime-watch.json"
PAGES = 30
PER_PAGE = 50
SAMPLE = 14

provider = AmiAmiProvider()


def still_listed(code: str) -> tuple[bool, str]:
    """Does this product code still return a live listing?"""
    payload = provider._decode(
        provider.request("GET", API_ROOT + "/item", params={"gcode": code, "lang": "eng"})
    )
    raw = payload.get("item") or {}
    if not raw:
        return False, "no longer listed"
    closed = bool(raw.get("order_closed_flg")) or bool(raw.get("end_flg"))
    if closed or not raw.get("stock"):
        return False, "listed but sold out"
    return True, f"still on sale at {raw.get('price') or raw.get('price1')}"


def main() -> None:
    state = json.loads(WATCH.read_text(encoding="utf-8"))
    before = state["full"]["start"]["listings"]
    after = state["full"]["end"]["listings"]
    arrived = {code for code in after if code not in before}

    seen: set[str] = set()
    for page in range(1, PAGES + 1):
        payload = provider._decode(
            provider.request(
                "GET",
                API_ROOT + "/items",
                params={
                    "pagemax": PER_PAGE,
                    "pagecnt": page,
                    "s_cate_tag": 1,
                    "s_st_condition_flg": 1,
                    "s_sortkey": "preowned",
                    "lang": "eng",
                },
            )
        )
        items = payload.get("items") or []
        if not items:
            break
        seen.update(i.get("gcode") for i in items if i.get("gcode"))
        time.sleep(random.uniform(5, 9))

    missed = sorted(arrived - seen)
    print(f"  {len(missed):,} arrivals were not in the first {PAGES} pages")
    print(f"  Asking the shop about {min(SAMPLE, len(missed))} of them at random\n")

    sample = random.sample(missed, min(SAMPLE, len(missed)))
    live = 0
    for code in sample:
        time.sleep(random.uniform(5, 9))
        try:
            ok, why = still_listed(code)
        except Exception as exc:  # noqa: BLE001
            ok, why = False, f"lookup failed: {exc}"
        live += 1 if ok else 0
        print(f"    {code:<26}{why}")

    gone = len(sample) - live
    print(f"\n  {gone} of {len(sample)} have gone; {live} are still buyable.")
    if sample:
        implied = 452 + (len(missed) * live / len(sample))
        print(
            f"  Scaled up, roughly {len(missed) * live / len(sample):.0f} of the "
            f"{len(missed)} missed are still on sale,\n"
            f"  so the head's recall among listings that still exist is about "
            f"{452 / implied * 100:.0f}%."
        )


if __name__ == "__main__":
    main()
