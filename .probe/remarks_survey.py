"""What does AmiAmi actually put in the red text on a pre-owned page?

Two kinds share one field. A condition note says why a used copy is cheap -
"[Discoloration] Upper body skin area has become white". A logistics note says
the parcel is awkward - "*Shipping costs for this item may be very high due to
package size or/and weight". Showing the second one as a defect would be worse
than showing nothing, so the two have to be told apart on more evidence than
the two examples that prompted this.

Samples the cheapest pre-owned listings, which skew towards the low grades
that carry these notes, and prints every distinct red passage it finds so the
vocabulary can be read rather than guessed at.

    python .probe/remarks_survey.py
"""
from __future__ import annotations

import os
import pathlib
import random
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
os.environ.setdefault("SECRET_KEY", "remarks-survey-not-a-real-secret")

from app.providers.amiami import API_ROOT, AmiAmiProvider  # noqa: E402

SAMPLE = 110
provider = AmiAmiProvider()

RED = re.compile(r"<font[^>]*color[^>]*red[^>]*>(.*?)</font>", re.IGNORECASE | re.DOTALL)
TAGS = re.compile(r"<[^>]+>")


def red_parts(remarks: str | None) -> list[str]:
    return [" ".join(TAGS.sub("", m).split()) for m in RED.findall(remarks or "")]


def main() -> None:
    # Sampled by grade, not by price. The first attempt took the cheapest
    # listings, which are cheap small things rather than marked-down figures,
    # and turned up one red passage in forty. The grade is in the title:
    # "(Pre-owned ITEM:C/BOX:B)Azur Lane Atago...".
    codes: list[str] = []
    for page in range(1, 9):
        payload = provider._decode(
            provider.request(
                "GET",
                API_ROOT + "/items",
                params={
                    "pagemax": 50,
                    "pagecnt": page,
                    "s_cate_tag": 1,
                    "s_st_condition_flg": 1,
                    "s_sortkey": "preowned",
                    "lang": "eng",
                },
            )
        )
        # The grade is not in the list response at all - gname is the
        # product name, and the condition lives in the per-copy sname that
        # only a detail fetch returns. So sample broadly and read what comes.
        codes += [i.get("gcode") for i in (payload.get("items") or []) if i.get("gcode")]
        print(f"    page {page}: {len(codes)} listings pooled")
        if len(codes) >= SAMPLE:
            break
        time.sleep(random.uniform(5, 9))

    random.shuffle(codes)
    found: list[tuple[str, str]] = []
    for code in codes[:SAMPLE]:
        time.sleep(random.uniform(5, 9))
        try:
            payload = provider._decode(
                provider.request(
                    "GET", API_ROOT + "/item", params={"gcode": code, "lang": "eng"}
                )
            )
        except Exception:  # noqa: BLE001
            continue
        raw = payload.get("item") or {}
        for part in red_parts(raw.get("remarks")):
            if part:
                found.append((code, part))

    print(f"  {len(found)} red passage(s) across {SAMPLE} sampled listings\n")

    bracketed = [p for _, p in found if p.startswith("[")]
    starred = [p for _, p in found if p.startswith("*")]
    other = [p for _, p in found if not p.startswith(("[", "*"))]

    for label, group in (
        ("Starting with a [bracketed] tag", bracketed),
        ("Starting with an asterisk", starred),
        ("Neither", other),
    ):
        print(f"  --- {label}: {len(group)} ---")
        for text in sorted(set(group))[:14]:
            print(f"      {text[:150]}")
        print()

    # The opening tag is the thing worth knowing: if condition notes always
    # carry one and logistics notes never do, that is the whole rule.
    tags = sorted({p.split("]")[0][1:] for p in bracketed if "]" in p})
    print(f"  Distinct bracketed tags seen: {', '.join(tags) if tags else 'none'}")


if __name__ == "__main__":
    main()
