"""Does any ordering put freshly taken-in used copies at the front?

We have a record of what actually arrived: a 24.7-hour watch that enumerated
the whole pre-owned slice at both ends, so the set of listings present at the
end but not at the start is known - 592 of them. Under "regtimed" only 9 of
those 592 ever showed up in the first 20 pages.

That measurement can be reused. Rather than waiting another day, ask each
ordering for its first pages now and see how many of those known arrivals sit
there. An ordering that tracks intake should still be holding them near the
front a day later; one that does not will scatter them.

It is not as strong as a fresh longitudinal run - a copy that has since sold
is gone from every ordering, which drags every score down equally - but it is
the same test applied to every candidate at once, for twenty requests each
instead of a day.

    python .probe/intake_order.py
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
os.environ.setdefault("SECRET_KEY", "intake-order-not-a-real-secret")

from app.providers.amiami import API_ROOT, AmiAmiProvider  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
WATCH = HERE / "regtime-watch.json"
HEAD_PAGES = 20
PER_PAGE = 50

CANDIDATES = ["regtimed", "preowned", "", "releasedated"]

provider = AmiAmiProvider()


def head(sortkey: str) -> dict[str, int]:
    """gcode -> position, over the first pages of one ordering."""
    positions: dict[str, int] = {}
    for page in range(1, HEAD_PAGES + 1):
        params = {
            "pagemax": PER_PAGE,
            "pagecnt": page,
            "s_cate_tag": 1,
            "s_st_condition_flg": 1,
            "lang": "eng",
        }
        if sortkey:
            params["s_sortkey"] = sortkey
        payload = provider._decode(provider.request("GET", API_ROOT + "/items", params=params))
        items = payload.get("items") or []
        if not items:
            break
        for offset, raw in enumerate(items):
            code = raw.get("gcode")
            if code and code not in positions:
                positions[code] = (page - 1) * PER_PAGE + offset + 1
        time.sleep(random.uniform(5, 9))
    return positions


def main() -> None:
    if not WATCH.exists():
        print("No watch data. Run .probe/regtime-watch.bat first.")
        return

    state = json.loads(WATCH.read_text(encoding="utf-8"))
    before = state["full"]["start"]["listings"]
    after = state["full"]["end"]["listings"]
    arrived = [code for code in after if code not in before]
    ended = datetime.fromisoformat(state["full"]["end"]["at"])
    age = (datetime.now(timezone.utc) - ended).total_seconds() / 3600

    print(f"  {len(arrived):,} listings arrived during the watch, which ended "
          f"{age:.0f} hours ago")
    print(f"  Asking each ordering for its first {HEAD_PAGES} pages "
          f"({HEAD_PAGES * PER_PAGE:,} slots)\n")

    print(f"  {'ordering':<16}{'of the arrivals':<18}{'still listed':<14}chance")
    print(f"  {'-' * 72}")

    for key in CANDIDATES:
        positions = head(key)
        found = [code for code in arrived if code in positions]
        # What a random slice of the catalogue that size would have caught,
        # so a hit rate can be read against something.
        total = len(after)
        expected = len(arrived) * (len(positions) / total) if total else 0
        factor = (len(found) / expected) if expected else 0
        label = key or "(default)"
        print(
            f"  {label:<16}{len(found):>4} of {len(arrived):<10}"
            f"{len(positions):>6} slots  "
            f"{factor:>5.2f}x chance"
        )

    print(
        "\n  1.00x means the ordering is no better than picking that many\n"
        "  listings at random. Anything that genuinely puts new intake first\n"
        "  would be far above it."
    )


if __name__ == "__main__":
    main()
