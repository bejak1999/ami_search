"""How deep into the "preowned" ordering the new arrivals actually sit.

The comparison in intake_order.py found that the 中古 ordering holds half the
known arrivals in its first 20 pages, against a twentieth of them under
"regtimed". That settles which ordering to read; it does not say how much of
it to read. This walks further and reports the recall page by page, so the
head size is chosen from the falloff rather than guessed.

    python .probe/preowned_depth.py
"""
from __future__ import annotations

import json
import os
import pathlib
import random
import sys
import time
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
os.environ.setdefault("SECRET_KEY", "preowned-depth-not-a-real-secret")

from app.providers.amiami import API_ROOT, AmiAmiProvider  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
WATCH = HERE / "regtime-watch.json"
PAGES = 40
PER_PAGE = 50

provider = AmiAmiProvider()


def main() -> None:
    state = json.loads(WATCH.read_text(encoding="utf-8"))
    before = state["full"]["start"]["listings"]
    after = state["full"]["end"]["listings"]
    arrived = {code for code in after if code not in before}
    ended = datetime.fromisoformat(state["full"]["end"]["at"])
    age = (datetime.now(timezone.utc) - ended).total_seconds() / 3600

    positions: dict[str, int] = {}
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
        for offset, raw in enumerate(items):
            code = raw.get("gcode")
            if code and code not in positions:
                positions[code] = (page - 1) * PER_PAGE + offset + 1
        time.sleep(random.uniform(5, 9))

    hits = sorted(positions[c] for c in arrived if c in positions)
    print(f"  {len(arrived):,} arrivals recorded, the window closed {age:.0f} hours ago")
    print(f"  {len(hits):,} of them are inside the first {PAGES} pages\n")
    print(f"  {'pages read':<14}{'requests/h at 60 min':<24}{'arrivals found':<18}share")
    print(f"  {'-' * 70}")
    for pages in (2, 5, 10, 20, 30, 40):
        within = sum(1 for p in hits if p <= pages * PER_PAGE)
        print(
            f"  first {pages:<8}{pages:>10} an hour      "
            f"{within:>5} of {len(arrived):<8}{within / len(arrived) * 100:>5.0f}%"
        )
    print(
        f"\n  The full slice is {-(-len(after) // PER_PAGE)} pages. Anything the head misses is"
        "\n  still picked up by the sweep, just later."
    )


if __name__ == "__main__":
    main()
