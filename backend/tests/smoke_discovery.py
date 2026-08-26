"""End-to-end test of the MyFigureCollection cross-reference and discovery.

Hits both AmiAmi and MyFigureCollection for real.
Run with:  python tests/smoke_discovery.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# Make "app" importable however this file is invoked, so the suites run with a
# plain "python tests/<name>.py" and do not depend on PYTHONPATH being set.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = tempfile.mkdtemp(prefix="amisearch-disco-")
os.environ["DATA_DIR"] = TMP
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, condition: bool, extra: object = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {extra}")


def main() -> int:
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/register",
            json={"email": "d@example.com", "username": "disco", "password": "Discovery-2026!"},
        )
        auth = {"Authorization": f"Bearer {r.json()['token']}"}

        print("\n== resolve an AmiAmi item with a barcode ==")
        r = client.post(
            "/api/search/resolve",
            headers=auth,
            json={"input": "FIGURE-153570-R"},
        )
        check("resolve 200", r.status_code == 200, r.text[:200])
        item = r.json()
        item_id = item["id"]
        check("AmiAmi supplied a JAN code", bool(item.get("jan_code")), item.get("jan_code"))
        print(f"         -> {item['code']} JAN={item.get('jan_code')}")

        print("\n== link it to MyFigureCollection ==")
        r = client.post(f"/api/discover/item/{item_id}/enrich", headers=auth)
        check("enrich 200", r.status_code == 200, r.text[:300])
        detail = r.json().get("detail") or {}
        check("matched by barcode", detail.get("matched_by") == "jan", r.json())
        check("full confidence on a barcode match", detail.get("confidence") == 1.0, detail)
        check("tags imported", (detail.get("tags") or 0) > 10, detail)
        print(f"         -> MFC {detail.get('mfc_id')} with {detail.get('tags')} tags")

        print("\n== the tag index ==")
        r = client.get(f"/api/discover/item/{item_id}/tags", headers=auth)
        check("item tags 200", r.status_code == 200, r.text[:200])
        tags = r.json()
        kinds = {t["kind"] for t in tags}
        check("entries classified into kinds", {"origin", "character", "company"} <= kinds, kinds)
        check("plain tags present", "tag" in kinds, kinds)
        slugs = [t["slug"] for t in tags if t["kind"] == "tag"]
        print(f"         -> {len(tags)} tags across {sorted(kinds)}")
        print(f"         -> sample: {slugs[:8]}")

        r = client.get("/api/discover/tags?q=blue", headers=auth)
        check("tag autocomplete 200", r.status_code == 200, r.text[:200])
        check("autocomplete finds something", len(r.json()) > 0, r.json())

        print("\n== MFC data backfilled the shop record ==")
        r = client.get(f"/api/items/{item_id}", headers=auth)
        enriched = r.json()
        check("series filled in", bool(enriched.get("series")), enriched.get("series"))
        check("character filled in", bool(enriched.get("character")), enriched.get("character"))
        check("MFC link exposed", bool(enriched.get("mfc_url")), enriched.get("mfc_url"))
        print(f"         -> {enriched.get('series')} / {enriched.get('character')}")

        print("\n== local discovery by tag ==")
        if slugs:
            r = client.get("/api/discover/local", headers=auth, params={"tags": slugs[:1]})
            check("local discovery 200", r.status_code == 200, r.text[:200])
            check("finds the enriched item", any(i["id"] == item_id for i in r.json()), len(r.json()))

            r = client.get(
                "/api/discover/local",
                headers=auth,
                params={"tags": ["__definitely_not_a_tag__"]},
            )
            check("unknown tag returns nothing", r.json() == [], r.json())

            if len(slugs) >= 2:
                r = client.get("/api/discover/local", headers=auth, params={"tags": slugs[:2]})
                check("multi-tag AND still matches", any(i["id"] == item_id for i in r.json()))

        print("\n== stats ==")
        r = client.get("/api/discover/stats", headers=auth)
        check("stats 200", r.status_code == 200, r.text[:200])
        stats = r.json()["detail"]
        check("counts the linked item", stats["linked_items"] >= 1, stats)
        check("top tags listed", len(stats["top_tags"]) > 0, stats)

        print("\n== MFC tag browse bridged to the shop (slow path) ==")
        r = client.post(
            "/api/discover/mfc",
            headers=auth,
            json={"tags": ["battle_pose", "sword"], "figures_only": False, "lookups": 3},
        )
        check("mfc discovery 200", r.status_code == 200, r.text[:300])
        if r.status_code == 200:
            detail = r.json()["detail"]
            check("returned MFC listings", len(detail["results"]) > 0, detail.get("total_pages"))
            check("stayed within the lookup budget", detail["shop_lookups_used"] <= 3, detail)
            matched = [x for x in detail["results"] if x["item"]]
            print(f"         -> {len(detail['results'])} MFC items, {len(matched)} found on AmiAmi")
            for row in detail["results"][:4]:
                mark = "OK " if row["item"] else "-- "
                shop = row["item"]["code"] if row["item"] else row["state"]
                print(f"            {mark} {row['mfc_title'][:58]:<58} {shop}")

        print("\n== batch enrichment ==")
        client.post("/api/search", headers=auth, json={"q": "nendoroid", "per_page": 5})
        r = client.post("/api/discover/enrich/run?limit=2", headers=auth)
        check("batch enrichment 200", r.status_code == 200, r.text[:200])
        print(f"         -> {r.json()['message']}")

    print(f"\n{'=' * 46}\n  {PASS} passed, {FAIL} failed\n{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
