"""End-to-end smoke test against the real AmiAmi API.

Run with:  python tests/smoke_e2e.py
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

TMP = tempfile.mkdtemp(prefix="amisearch-test-")
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
        print("\n== public config ==")
        r = client.get("/api/config")
        check("GET /api/config", r.status_code == 200, r.text[:200])
        check("no users yet", r.json()["has_users"] is False)
        check("registration open", r.json()["registration_open"] is True)

        print("\n== registration ==")
        r = client.post(
            "/api/auth/register",
            json={"email": "hunter@example.com", "username": "hunter", "password": "Figure-Hunt-2026"},
        )
        check("register 201", r.status_code == 201, r.text[:300])
        check("first user is admin", r.json()["user"]["role"] == "admin")
        token = r.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        r = client.post(
            "/api/auth/register",
            json={"email": "weak@example.com", "username": "weak", "password": "short"},
        )
        check("weak password rejected", r.status_code == 422, r.status_code)

        r = client.post(
            "/api/auth/register",
            json={"email": "hunter@example.com", "username": "other", "password": "Figure-Hunt-2026"},
        )
        check("duplicate e-mail rejected", r.status_code == 409, r.status_code)

        print("\n== auth ==")
        # The register call set a session cookie, so this client is signed in
        # even without the bearer header. A separate client proves the gate.
        check("cookie session works", client.get("/api/auth/me").status_code == 200)
        with TestClient(app) as anon:
            check("anonymous /me is 401", anon.get("/api/auth/me").status_code == 401)
        r = client.get("/api/auth/me", headers=auth)
        check("authenticated /me", r.status_code == 200 and r.json()["username"] == "hunter")

        r = client.post(
            "/api/auth/login",
            json={"identifier": "hunter@example.com", "password": "wrong-password"},
        )
        check("bad password rejected", r.status_code == 401)

        print("\n== live search (AmiAmi) ==")
        r = client.post(
            "/api/search",
            headers=auth,
            json={"q": "symphogear tsubasa", "condition": "preowned", "per_page": 8},
        )
        check("search 200", r.status_code == 200, r.text[:300])
        payload = r.json()
        check("search returned items", len(payload["items"]) > 0, payload.get("total"))
        first = payload["items"][0] if payload["items"] else {}
        check("item has a price", first.get("price") is not None, first)
        check("item persisted with an id", first.get("id") is not None)
        check("item is pre-owned", first.get("condition") == "preowned", first.get("condition"))
        check(
            "landed cost computed",
            isinstance(first.get("landed"), dict) and first["landed"]["total"] > 0,
            first.get("landed"),
        )
        print(
            f"         -> {first.get('code')} {first.get('price')} JPY "
            f"= {first.get('landed', {}).get('total')} EUR landed"
        )

        print("\n== resolve a pasted link ==")
        r = client.post(
            "/api/search/resolve",
            headers=auth,
            json={"input": "https://www.amiami.com/eng/detail/?gcode=FIGURE-153570-R"},
        )
        check("resolve 200", r.status_code == 200, r.text[:300])
        resolved = r.json()
        check("resolved the right code", resolved.get("code") == "FIGURE-153570-R")
        check("gallery loaded", len(resolved.get("images") or []) > 1, len(resolved.get("images") or []))
        item_id = resolved["id"]

        print("\n== price history ==")
        r = client.get(f"/api/items/{item_id}/history", headers=auth)
        check("history 200", r.status_code == 200, r.text[:200])
        check("history has a point", len(r.json()["points"]) >= 1)

        r = client.get(f"/api/items/{item_id}/landed", headers=auth)
        check("landed breakdown 200", r.status_code == 200, r.text[:200])
        breakdown = r.json() if r.status_code == 200 else {}
        check(
            "breakdown adds up",
            abs(
                breakdown.get("total", 0)
                - sum(breakdown.get(k, 0) for k in ("goods", "shipping", "duty", "vat", "handling"))
            )
            < 0.02,
            breakdown,
        )
        print(
            f"         -> goods {breakdown.get('goods')} + ship {breakdown.get('shipping')} "
            f"+ duty {breakdown.get('duty')} + vat {breakdown.get('vat')} "
            f"+ fee {breakdown.get('handling')} = {breakdown.get('total')} EUR"
        )

        print("\n== cost profile drives the landed price ==")
        r = client.patch(
            "/api/auth/cost-profile",
            headers=auth,
            json={"shipping_mode": "none", "duty_rate": 0.0, "vat_rate": 0.0, "customs_handling_fee": 0.0},
        )
        check("cost profile patched", r.status_code == 200, r.text[:200])
        r = client.get(f"/api/items/{item_id}/landed", headers=auth)
        bare = r.json()
        check("no shipping, no tax means total equals goods", abs(bare["total"] - bare["goods"]) < 0.01, bare)
        client.patch(
            "/api/auth/cost-profile",
            headers=auth,
            json={"shipping_mode": "table", "duty_rate": 0.047, "vat_rate": 0.19, "customs_handling_fee": 6.0},
        )

        print("\n== watches ==")
        r = client.post(
            "/api/watches",
            headers=auth,
            json={
                "label": "Symphogear pre-owned under target",
                "kind": "search",
                "query": "symphogear",
                "condition": "preowned",
                "target_price": 200.0,
                "price_basis": "landed",
                "target_currency": "EUR",
                "interval_seconds": 300,
            },
        )
        check("create search watch 201", r.status_code == 201, r.text[:300])
        watch = r.json()
        check("target stored", watch["target_price"] == 200.0)
        check("basis stored", watch["price_basis"] == "landed")
        watch_id = watch["id"]

        r = client.post(
            "/api/watches",
            headers=auth,
            json={
                "label": "",
                "kind": "item",
                "item_code": "https://www.amiami.com/eng/detail/?gcode=FIGURE-153570-R",
                "target_price": 9000,
                "price_basis": "listed",
                "target_currency": "JPY",
            },
        )
        check("create item watch from URL 201", r.status_code == 201, r.text[:300])
        check("URL reduced to a product code", r.json()["item_code"] == "FIGURE-153570-R", r.json().get("item_code"))
        item_watch_id = r.json()["id"]

        r = client.post("/api/watches", headers=auth, json={"kind": "search", "query": "   "})
        check("empty search watch rejected", r.status_code == 400, r.status_code)

        print("\n== running a watch ==")
        r = client.post(f"/api/watches/{watch_id}/run", headers=auth)
        check("run 200", r.status_code == 200, r.text[:300])
        detail = r.json()["detail"]
        print(f"         -> {detail}")
        check("watch checked listings", detail["checked"] > 0, detail)
        check("scheduler set a next run", detail["next_run_at"] is not None)

        r = client.post(f"/api/watches/{watch_id}/run", headers=auth)
        second = r.json()["detail"]
        check("second run stays quiet, nothing crossed the target", second["alerts"] == 0, second)

        r = client.post(f"/api/watches/{item_watch_id}/preview", headers=auth)
        check("preview 200", r.status_code == 200, r.text[:200])

        print("\n== alerts ==")
        r = client.get("/api/alerts", headers=auth)
        check("alerts 200", r.status_code == 200)
        alerts = r.json()
        print(f"         -> {len(alerts)} alert(s) raised")
        if alerts:
            check("alert carries a landed price", alerts[0].get("landed_price") is not None, alerts[0])
            rr = client.post(f"/api/alerts/{alerts[0]['id']}/read", headers=auth)
            check("mark read", rr.status_code == 200 and rr.json()["read_at"] is not None)

        r = client.get("/api/alerts/unread-count", headers=auth)
        check("unread count 200", r.status_code == 200, r.text[:200])

        print("\n== notification channels ==")
        r = client.get("/api/channels/types", headers=auth)
        types = {t["type"] for t in r.json()}
        check(
            "all seven channel types offered",
            types == {"telegram", "webpush", "email", "discord", "ntfy", "gotify", "webhook"},
            types,
        )

        r = client.post(
            "/api/channels",
            headers=auth,
            json={"type": "telegram", "name": "My phone", "config": {"bot_token": "123:abc", "chat_id": "42"}},
        )
        check("create channel 201", r.status_code == 201, r.text[:300])
        channel = r.json()
        check("secrets redacted in responses", "abc" not in str(channel["config_preview"]), channel["config_preview"])
        channel_id = channel["id"]

        r = client.post("/api/channels", headers=auth, json={"type": "ntfy", "config": {}})
        check("missing required field rejected", r.status_code == 422, r.status_code)

        r = client.patch(f"/api/channels/{channel_id}", headers=auth, json={"config": {"chat_id": "99"}})
        check("partial update keeps the stored token", r.status_code == 200, r.text[:200])

        r = client.post(f"/api/channels/{channel_id}/test", headers=auth)
        check("test send fails cleanly on a fake token", r.status_code == 400, r.status_code)

        print("\n== wishlist ==")
        r = client.post(
            "/api/collection",
            headers=auth,
            json={"item_id": item_id, "status": "wishlist", "priority": 1, "tags": ["grail"]},
        )
        check("add to wishlist 201", r.status_code == 201, r.text[:300])
        entry_id = r.json()["id"]

        r = client.get("/api/collection?status=wishlist", headers=auth)
        check("wishlist lists the entry", len(r.json()) == 1, r.json())

        r = client.get("/api/collection/summary", headers=auth)
        check("summary 200", r.status_code == 200, r.text[:200])
        check("wishlist landed total computed", r.json()["detail"]["wishlist_landed_total"] > 0, r.json()["detail"])

        r = client.get("/api/collection/export?fmt=csv", headers=auth)
        check("csv export", r.status_code == 200 and "code" in r.text, r.text[:100])

        r = client.patch(f"/api/collection/{entry_id}", headers=auth, json={"status": "owned", "paid_price": 10380})
        check("mark as owned", r.status_code == 200 and r.json()["status"] == "owned")

        print("\n== dashboard and status ==")
        r = client.get("/api/dashboard", headers=auth)
        check("dashboard 200", r.status_code == 200, r.text[:300])
        stats = r.json()
        check("counts watches", stats["watches_total"] == 2, stats["watches_total"])
        check("counts tracked items", stats["items_tracked"] > 0, stats["items_tracked"])

        r = client.get("/api/status", headers=auth)
        check("status 200", r.status_code == 200, r.text[:200])
        status_payload = r.json()
        check("provider listed", status_payload["providers"][0]["id"] == "amiami")
        check("fx rates loaded", bool(status_payload["fx"]["rates"]), status_payload["fx"])

        print("\n== multi-user isolation ==")
        r = client.post(
            "/api/auth/register",
            json={"email": "second@example.com", "username": "second", "password": "Second-User-2026"},
        )
        check("second user registers", r.status_code == 201, r.text[:200])
        check("second user is not admin", r.json()["user"]["role"] == "user")
        auth2 = {"Authorization": f"Bearer {r.json()['token']}"}

        check("second user sees no watches", client.get("/api/watches", headers=auth2).json() == [])
        check(
            "second user cannot open another user's watch",
            client.get(f"/api/watches/{watch_id}", headers=auth2).status_code == 404,
        )
        check(
            "non-admin blocked from admin routes",
            client.get("/api/admin/users", headers=auth2).status_code == 403,
        )
        check("admin can list users", len(client.get("/api/admin/users", headers=auth).json()) == 2)

        print("\n== cleanup ==")
        check("delete watch", client.delete(f"/api/watches/{watch_id}", headers=auth).status_code == 200)
        check("logout", client.post("/api/auth/logout", headers=auth).status_code == 200)
        check("token revoked after logout", client.get("/api/auth/me", headers=auth).status_code == 401)

    print(f"\n{'=' * 46}\n  {PASS} passed, {FAIL} failed\n{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
