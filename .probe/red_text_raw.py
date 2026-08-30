"""Save the red text verbatim, so the classifier can be tested on real bytes.

The survey printed these with whitespace collapsed, which hid the one thing
that matters: what separates two statements inside one red block. AmiAmi's own
examples suggest a newline - "...has become white\\nBoth legs are sticky" - and
a classifier that splits on the wrong thing fails quietly, keeping a shipping
notice or dropping a defect.

    python .probe/red_text_raw.py

Writes .probe/red-remarks.json, which test_offline reads if it is present.
"""
from __future__ import annotations

import json
import os
import pathlib
import random
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
os.environ.setdefault("SECRET_KEY", "red-text-not-a-real-secret")

from app.providers.amiami import API_ROOT, AmiAmiProvider  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "red-remarks.json"
SAMPLE = 90

RED = re.compile(r"<font[^>]*color[^>]*red[^>]*>", re.IGNORECASE)
provider = AmiAmiProvider()


def main() -> None:
    codes: list[str] = []
    for page in range(1, 4):
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
        codes += [i.get("gcode") for i in (payload.get("items") or []) if i.get("gcode")]
        time.sleep(random.uniform(5, 9))

    random.shuffle(codes)
    captured: list[dict] = []
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
        remarks = (payload.get("item") or {}).get("remarks")
        if remarks and RED.search(remarks):
            captured.append({"gcode": code, "remarks": remarks})
            print(f"    {len(captured):>2}  {code}")

    OUT.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  {len(captured)} of {SAMPLE} carried red text; written to {OUT.name}")

    newline = sum(1 for c in captured if "\n" in c["remarks"])
    br = sum(1 for c in captured if re.search(r"<br", c["remarks"], re.IGNORECASE))
    print(f"  {newline} contain a newline, {br} contain a <br> tag")


if __name__ == "__main__":
    main()
