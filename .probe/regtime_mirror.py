"""Is "regtimed" really the reverse of "regtimea"?

"regtimea" ascending hands back the oldest product records first - ids around
2,100, which is as far back as AmiAmi's catalogue goes. So the field is real
and the shop does order by it. If "regtimed" is the same field descending,
then its first page must be the last page of "regtimea", read backwards.

If it is, the ordering follows a scheme and the scheme is simply not the one
we want: a used copy taken in today does not create a new product record, so
it does not move to the front of a list ordered by when the record was made.

If it is not, "regtimed" is doing something else entirely, and the label
新着順 is describing an intention rather than the result.

    python .probe/regtime_mirror.py
"""
from __future__ import annotations

import os
import pathlib
import random
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
os.environ.setdefault("SECRET_KEY", "regtime-mirror-not-a-real-secret")

from app.providers.amiami import API_ROOT, AmiAmiProvider  # noqa: E402

PER_PAGE = 50
provider = AmiAmiProvider()


def fetch(sortkey: str, pagecnt: int) -> tuple[list[dict], int]:
    payload = provider._decode(
        provider.request(
            "GET",
            API_ROOT + "/items",
            params={
                "pagemax": PER_PAGE,
                "pagecnt": pagecnt,
                "s_cate_tag": 1,
                "s_st_condition_flg": 1,
                "s_sortkey": sortkey,
                "lang": "eng",
            },
        )
    )
    total = int((payload.get("search_result") or {}).get("total_results") or 0)
    return payload.get("items") or [], total


def ids(items: list[dict]) -> list[int | None]:
    out = []
    for raw in items:
        parts = (raw.get("gcode") or "").split("-")
        out.append(int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None)
    return out


def main() -> None:
    head_d, total = fetch("regtimed", 1)
    pages = -(-total // PER_PAGE)
    print(f"  {total:,} pre-owned figures over {pages} pages\n")

    time.sleep(random.uniform(6, 10))
    head_a, _ = fetch("regtimea", 1)
    time.sleep(random.uniform(6, 10))
    tail_a, _ = fetch("regtimea", pages)
    time.sleep(random.uniform(6, 10))
    tail_d, _ = fetch("regtimed", pages)

    def show(label: str, items: list[dict]) -> None:
        product_ids = [i for i in ids(items) if i]
        span = f"{min(product_ids):,}-{max(product_ids):,}" if product_ids else "-"
        print(f"  {label:<28}{len(items):>3} items   product ids {span}")
        for raw in items[:3]:
            print(f"      {raw.get('gcode'):<24}{str(raw.get('releasedate'))[:10]}")

    show("regtimea, first page", head_a)
    show("regtimea, last page", tail_a)
    show("regtimed, first page", head_d)
    show("regtimed, last page", tail_d)

    a_codes = [r.get("gcode") for r in tail_a]
    d_codes = [r.get("gcode") for r in head_d]
    overlap = set(a_codes) & set(d_codes)

    print()
    if d_codes == list(reversed(a_codes)):
        print("  The two are exact mirrors: one field, ordered both ways.")
    elif overlap:
        print(
            f"  Same end of the list from both directions ({len(overlap)} of "
            f"{len(d_codes)} shared), though not in mirrored order - which is\n"
            "  what ties look like: many records sharing one timestamp, broken\n"
            "  arbitrarily."
        )
    else:
        print(
            "  No overlap at all. The last page of the ascending order and the\n"
            "  first page of the descending order should be the same records;\n"
            "  they are not, so these are not one field read two ways."
        )


if __name__ == "__main__":
    main()
