"""Offline unit tests.

No network. These cover the parts where a silent mistake would be expensive:
the landed-cost arithmetic, the trigger rules that decide whether you get
woken up, and the parsers that turn shop responses into our model.

Run with:  python tests/test_offline.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8")

# Make "app" importable however this file is invoked, so the suites run with a
# plain "python tests/<name>.py" and do not depend on PYTHONPATH being set.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = tempfile.mkdtemp(prefix="amisearch-unit-")
os.environ["DATA_DIR"] = TMP
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ.setdefault("SECRET_KEY", "unit-test-secret")

PASS, FAIL = 0, 0


def check(label: str, condition: bool, extra: object = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {extra}")


def approx(a: float, b: float, tolerance: float = 0.01) -> bool:
    return abs(a - b) <= tolerance


def test_url_parsing() -> None:
    print("\n== AmiAmi URL and code parsing ==")
    from app.providers.amiami import AmiAmiProvider

    provider = AmiAmiProvider()
    cases = [
        ("https://www.amiami.com/eng/detail/?gcode=FIGURE-153570-R", "FIGURE-153570-R"),
        ("https://www.amiami.com/eng/detail/?gcode=FIGURE-153570-R&page=top", "FIGURE-153570-R"),
        ("www.amiami.com/eng/detail/?gcode=FIG-MOE-6590", "FIG-MOE-6590"),
        ("FIGURE-153570-R", "FIGURE-153570-R"),
        ("figure-153570-r", "FIGURE-153570-R"),
        ("https://example.com/nothing", None),
        ("", None),
        ("just some words", None),
    ]
    for raw, expected in cases:
        actual = provider.parse_url(raw)
        check(f"parse {raw[:48]!r} -> {expected}", actual == expected, actual)


def test_release_dates() -> None:
    print("\n== Release date normalisation ==")
    from app.providers.amiami import AmiAmiProvider

    provider = AmiAmiProvider()
    check("ISO timestamp", provider._release_label("2027-02-28 00:00:00") == "Feb-2027")
    check("short form passes through", provider._release_label("Apr-2023") == "Apr-2023")
    check("empty stays empty", provider._release_label(None) is None)
    check("garbage is preserved", provider._release_label("TBA") == "TBA")


def test_landed_cost() -> None:
    print("\n== Landed cost arithmetic ==")
    from app.db import SessionLocal, init_db
    from app.models import FxRate
    from app.services import landed_cost

    init_db()
    db = SessionLocal()
    db.add(FxRate(base="JPY", quote="EUR", rate=0.0055, source="test"))
    db.commit()

    profile = landed_cost.default_profile(user_id=1)
    profile.fx_markup = 0.0
    profile.shipping_mode = "flat"
    profile.shipping_flat = 20.0
    profile.vat_rate = 0.19
    profile.duty_rate = 0.047
    profile.duty_free_threshold = 150.0
    profile.customs_handling_fee = 6.0

    # 10,000 JPY at 0.0055 = 55 EUR goods, under the 150 EUR duty threshold.
    result = landed_cost.estimate(db, 10_000, "JPY", profile, "EUR")
    check("estimate returned", result is not None)
    assert result is not None
    check("goods converted", approx(result.goods, 55.0), result.goods)
    check("shipping applied", approx(result.shipping, 20.0), result.shipping)
    check("duty waived under the threshold", result.duty == 0.0 and result.duty_waived, result.duty)
    check("VAT on goods plus shipping", approx(result.vat, 75.0 * 0.19), result.vat)
    check("handling charged once tax applies", approx(result.handling, 6.0), result.handling)
    check(
        "total is the sum of its parts",
        approx(result.total, 55.0 + 20.0 + 0.0 + 14.25 + 6.0),
        result.total,
    )

    # 40,000 JPY = 220 EUR goods, now above the duty threshold.
    dutiable = landed_cost.estimate(db, 40_000, "JPY", profile, "EUR")
    assert dutiable is not None
    check("duty charged above the threshold", dutiable.duty > 0, dutiable.duty)
    check("duty is on goods plus shipping", approx(dutiable.duty, 240.0 * 0.047), dutiable.duty)
    check(
        "VAT compounds on top of duty",
        approx(dutiable.vat, (240.0 + 240.0 * 0.047) * 0.19),
        dutiable.vat,
    )

    # An unknown currency pair must degrade to None, not to a wrong number.
    check("unknown pair returns None", landed_cost.estimate(db, 100, "XYZ", profile, "EUR") is None)
    check("missing price returns None", landed_cost.estimate(db, None, "JPY", profile, "EUR") is None)

    # The inverse must round-trip.
    back = landed_cost.to_shop_currency(db, result.total, "EUR", "JPY", profile)
    check("inverse recovers the shop price", back is not None and approx(back, 10_000, 60), back)

    db.close()


def test_weight_estimation() -> None:
    print("\n== Shipping weight heuristics ==")
    from app.models import Item
    from app.services import landed_cost

    profile = landed_cost.default_profile(user_id=1)

    nendoroid = Item(name="Nendoroid Hatsune Miku", provider="amiami", code="A")
    scale_figure = Item(name="Azur Lane Honolulu 1/7 Complete Figure", provider="amiami", code="B")
    keychain = Item(name="Acrylic Keychain Miku", provider="amiami", code="C")
    unknown = Item(name="Mystery Object", provider="amiami", code="D")

    check("nendoroid is light", landed_cost.estimate_weight(nendoroid, profile) == 400)
    check("1/7 scale is heavy", landed_cost.estimate_weight(scale_figure, profile) == 1200)
    check("keychain is lightest", landed_cost.estimate_weight(keychain, profile) == 120)
    check("unknown falls back to the default", landed_cost.estimate_weight(unknown, profile) == 900)

    # An explicit height in the spec sheet should beat any keyword guess.
    sized = Item(name="Something", provider="amiami", code="E", spec="Scale: 1/7\nSize: H260mm")
    check("spec height wins", approx(landed_cost.estimate_weight(sized, profile), 1200, 40))

    tall = Item(name="Something", provider="amiami", code="F", spec="Size: H400mm")
    check(
        "a taller figure weighs more",
        landed_cost.estimate_weight(tall, profile) > landed_cost.estimate_weight(sized, profile),
    )


def test_shipping_table() -> None:
    print("\n== Shipping brackets ==")
    from app.services import landed_cost

    profile = landed_cost.default_profile(user_id=1)
    check("400 g lands in the first bracket", landed_cost.shipping_cost(400, profile)[0] == 14.0)
    check("900 g lands in the second", landed_cost.shipping_cost(900, profile)[0] == 20.0)
    check("1500 g lands in the third", landed_cost.shipping_cost(1500, profile)[0] == 30.0)
    check(
        "beyond the table it extrapolates",
        landed_cost.shipping_cost(20000, profile)[0] > 78.0,
        landed_cost.shipping_cost(20000, profile),
    )

    profile.shipping_mode = "none"
    check("excluded shipping is free", landed_cost.shipping_cost(5000, profile)[0] == 0.0)


def _valuation(compare: float | None):
    from app.services.matcher import Valuation

    return Valuation(
        compare_price=compare,
        compare_currency="EUR",
        landed_total=compare,
        landed_currency="EUR",
        breakdown=None,
    )


def test_trigger_rules() -> None:
    print("\n== When an alert fires ==")
    from app.models import Condition, Item, TriggerType, Watch, WatchSeenItem
    from app.services.matcher import decide_trigger

    def watch(**overrides) -> Watch:
        base = dict(
            user_id=1,
            target_price=100.0,
            notify_on_price_below=True,
            notify_on_restock=True,
            notify_on_new_match=True,
            notify_on_price_drop_pct=None,
        )
        base.update(overrides)
        return Watch(**base)

    def item(price=90.0, in_stock=True, condition=Condition.new) -> Item:
        return Item(
            provider="amiami",
            code="X",
            name="Test",
            current_price=price,
            in_stock=in_stock,
            condition=condition,
        )

    # A listing that did not exist before and is already under target is news.
    check(
        "new listing under target fires price_below",
        decide_trigger(watch(), item(), None, _valuation(90.0)) == TriggerType.price_below,
    )

    # The same listing on the next poll, still under target, is not news.
    seen = WatchSeenItem(watch_id=1, item_id=1, last_price=90.0, last_compare_price=90.0, last_in_stock=True)
    check(
        "already-below stays quiet",
        decide_trigger(watch(), item(), seen, _valuation(90.0)) is None,
    )

    # Crossing the line downward is news again.
    was_above = WatchSeenItem(
        watch_id=1, item_id=1, last_price=120.0, last_compare_price=120.0, last_in_stock=True
    )
    check(
        "crossing below the target fires",
        decide_trigger(watch(), item(), was_above, _valuation(90.0)) == TriggerType.price_below,
    )

    # Above target, nothing to say.
    check(
        "above target stays quiet",
        decide_trigger(watch(), item(price=150.0), was_above, _valuation(150.0)) is None,
    )

    # Restock beats a price drop in priority but loses to hitting the target.
    out_of_stock = WatchSeenItem(
        watch_id=1, item_id=1, last_price=150.0, last_compare_price=150.0, last_in_stock=False
    )
    check(
        "back in stock is reported",
        decide_trigger(watch(target_price=None), item(price=150.0), out_of_stock, _valuation(150.0))
        == TriggerType.back_in_stock,
    )
    check(
        "pre-owned restock is its own trigger",
        decide_trigger(
            watch(target_price=None),
            item(price=150.0, condition=Condition.preowned),
            out_of_stock,
            _valuation(150.0),
        )
        == TriggerType.restock_preowned,
    )
    check(
        "hitting the target outranks a restock",
        decide_trigger(watch(), item(price=90.0), out_of_stock, _valuation(90.0))
        == TriggerType.price_below,
    )


def test_trigger_switches() -> None:
    print("\n== Trigger switches are respected ==")
    from app.models import Condition, Item, TriggerType, Watch, WatchSeenItem
    from app.services.matcher import decide_trigger

    item = Item(
        provider="amiami",
        code="X",
        name="Test",
        current_price=90.0,
        in_stock=True,
        condition=Condition.new,
    )

    muted = Watch(
        user_id=1,
        target_price=100.0,
        notify_on_price_below=False,
        notify_on_restock=False,
        notify_on_new_match=False,
    )
    check("everything off means silence", decide_trigger(muted, item, None, _valuation(90.0)) is None)

    # A percentage drop that never reaches the target is still worth knowing.
    drop_watch = Watch(
        user_id=1,
        target_price=None,
        notify_on_price_below=False,
        notify_on_restock=False,
        notify_on_new_match=False,
        notify_on_price_drop_pct=20.0,
    )
    seen = WatchSeenItem(watch_id=1, item_id=1, last_price=200.0, last_compare_price=200.0, last_in_stock=True)
    item.current_price = 150.0
    check(
        "a 25% drop fires",
        decide_trigger(drop_watch, item, seen, _valuation(150.0)) == TriggerType.price_drop,
    )
    item.current_price = 190.0
    check(
        "a 5% drop does not",
        decide_trigger(drop_watch, item, seen, _valuation(190.0)) is None,
    )

    # An unbuyable listing is not a new match.
    unavailable = Item(
        provider="amiami",
        code="Y",
        name="Gone",
        current_price=50.0,
        in_stock=False,
        condition=Condition.preowned,
    )
    open_watch = Watch(user_id=1, target_price=None, notify_on_new_match=True, notify_on_restock=True)
    check(
        "an unavailable new listing is not announced",
        decide_trigger(open_watch, unavailable, None, _valuation(50.0)) is None,
    )


def test_grade_parsing() -> None:
    print()
    print("== Pre-owned condition grades ==")
    from app.providers.amiami import grade_rank, meets_grade, parse_grades

    check("detail form", parse_grades("ITEM:A/BOX:B") == ("A", "B"))
    check("other_items form", parse_grades("Condition Item:B Box:B") == ("B", "B"))
    check("B+ is kept whole", parse_grades("Condition Item:B+  Box:B") == ("B+", "B"))
    check("ideographic space", parse_grades("Condition Item:S　Box:A") == ("S", "A"))
    check("nothing to find", parse_grades("no grades here") == (None, None))
    check("empty input", parse_grades(None) == (None, None))

    check("S outranks A", grade_rank("S") < grade_rank("A"))
    check("A outranks B+", grade_rank("A") < grade_rank("B+"))
    check("B+ outranks B", grade_rank("B+") < grade_rank("B"))
    check("unknown sorts last", grade_rank("Z") > grade_rank("D"))

    check("A satisfies A", meets_grade("A", "A"))
    check("S satisfies A", meets_grade("S", "A"))
    check("B+ does not satisfy A", not meets_grade("B+", "A"))
    check("no filter accepts anything", meets_grade("D", None))
    check("unknown grade fails a filter", not meets_grade(None, "B"))


def test_variant_selection() -> None:
    print()
    print("== Choosing which graded listing to judge ==")
    from app.models import Condition, Item, Watch
    from app.services.matcher import effective_price, qualifying_variant

    # Modelled on FIGURE-165063-R, which really is sold at five grades at once.
    item = Item(
        provider="amiami",
        code="FIGURE-165063-R",
        name="Rias Gremory Bunny 1/4",
        currency="JPY",
        current_price=40180,
        price_max=59980,
        condition=Condition.preowned,
        variants=[
            {"code": "R151", "price": 40180, "item_grade": "B", "box_grade": "B"},
            {"code": "R153", "price": 53980, "item_grade": "B+", "box_grade": "B"},
            {"code": "R152", "price": 53980, "item_grade": "B+", "box_grade": "B"},
            {"code": "R156", "price": 59980, "item_grade": "A", "box_grade": "B"},
        ],
    )

    unfiltered = Watch(user_id=1)
    price, variant = effective_price(item, unfiltered)
    check("without a filter, the cheapest listing is judged", price == 40180, price)
    check("no variant is singled out", variant is None)

    wants_a = Watch(user_id=1, min_item_grade="A")
    price, variant = effective_price(item, wants_a)
    check("Item:A costs more", price == 59980, price)
    check("and names the listing", variant is not None and variant["code"] == "R156")

    wants_bplus = Watch(user_id=1, min_item_grade="B+")
    price, _ = effective_price(item, wants_bplus)
    check("Item:B+ picks the cheapest of the two", price == 53980, price)

    wants_box_a = Watch(user_id=1, min_box_grade="A")
    price, variant = effective_price(item, wants_box_a)
    check("no box is good enough, so no price", price is None, price)
    check("and no listing is returned", variant is None)

    check("box filter alone finds nothing here", qualifying_variant(item, wants_box_a) is None)

    bare = Item(provider="amiami", code="X", name="No variants", current_price=1000, variants=[])
    price, _ = effective_price(bare, wants_a)
    check("unknown grades cannot satisfy a filter", price is None, price)
    price, _ = effective_price(bare, unfiltered)
    check("but are fine without one", price == 1000, price)


def test_grade_filter_blocks_alerts() -> None:
    print()
    print("== A grade filter changes whether an alert fires ==")
    from app.models import Condition, Item, TriggerType, Watch
    from app.services.matcher import Valuation, decide_trigger, effective_price

    item = Item(
        provider="amiami",
        code="FIGURE-165063-R",
        name="Rias Gremory Bunny 1/4",
        currency="JPY",
        current_price=40180,
        in_stock=True,
        condition=Condition.preowned,
        variants=[
            {"code": "R151", "price": 40180, "item_grade": "B", "box_grade": "B"},
            {"code": "R156", "price": 59980, "item_grade": "A", "box_grade": "B"},
        ],
    )

    def valuation_for(watch: Watch) -> Valuation:
        price, variant = effective_price(item, watch)
        return Valuation(
            compare_price=price,
            compare_currency="JPY",
            landed_total=None,
            landed_currency="EUR",
            breakdown=None,
            shop_price=price,
            variant=variant,
        )

    loose = Watch(user_id=1, target_price=45000, target_currency="JPY")
    check(
        "45k target matches the B-grade copy",
        decide_trigger(loose, item, None, valuation_for(loose)) == TriggerType.price_below,
    )

    strict = Watch(user_id=1, target_price=45000, target_currency="JPY", min_item_grade="A")
    check(
        "the same target stays quiet when only Item:A will do",
        decide_trigger(strict, item, None, valuation_for(strict)) is None,
    )

    generous = Watch(user_id=1, target_price=65000, target_currency="JPY", min_item_grade="A")
    check(
        "raising the target to 65k reaches the A-grade copy",
        decide_trigger(generous, item, None, valuation_for(generous)) == TriggerType.price_below,
    )


def test_quiet_hours() -> None:
    print("\n== Quiet hours ==")
    from app.models import User, Watch
    from app.services.notify import in_quiet_hours

    user = User(id=1, email="a@b.c", username="u", password_hash="x", timezone="UTC")
    watch = Watch(
        user_id=1,
        quiet_hours={"enabled": True, "start": "23:00", "end": "07:00", "urgent_override": True},
    )

    def at(hour: int) -> datetime:
        return datetime(2026, 1, 15, hour, 0, tzinfo=timezone.utc)

    check("03:00 is quiet", in_quiet_hours(watch, user, at(3)))
    check("23:30 is quiet", in_quiet_hours(watch, user, datetime(2026, 1, 15, 23, 30, tzinfo=timezone.utc)))
    check("12:00 is not quiet", not in_quiet_hours(watch, user, at(12)))
    check("07:00 is the end, so not quiet", not in_quiet_hours(watch, user, at(7)))

    daytime = Watch(user_id=1, quiet_hours={"enabled": True, "start": "09:00", "end": "17:00"})
    check("a same-day window works too", in_quiet_hours(daytime, user, at(12)))
    check("outside that window is loud", not in_quiet_hours(daytime, user, at(20)))

    off = Watch(user_id=1, quiet_hours={"enabled": False, "start": "23:00", "end": "07:00"})
    check("disabled means never quiet", not in_quiet_hours(off, user, at(3)))


def test_adaptive_intervals() -> None:
    print("\n== Adaptive polling ==")
    from app.config import settings
    from app.models import Watch
    from app.services.matcher import RunOutcome, next_interval

    outcome = RunOutcome(watch_id=1)

    manual = Watch(user_id=1, adaptive=False, interval_seconds=600)
    check("manual interval is honoured", next_interval(manual, outcome, False) == 600)

    below_floor = Watch(user_id=1, adaptive=False, interval_seconds=1)
    check(
        "the global floor cannot be undercut",
        next_interval(below_floor, outcome, False) == settings.min_poll_interval_seconds,
    )

    adaptive = Watch(user_id=1, adaptive=True, interval_seconds=600)
    hot = next_interval(adaptive, outcome, True)
    cold = next_interval(adaptive, outcome, False)
    check("a hot watch speeds up", hot < cold, (hot, cold))
    check("a hot watch respects the floor", hot >= settings.min_poll_interval_seconds, hot)

    failing = Watch(user_id=1, adaptive=True, interval_seconds=300, consecutive_errors=4)
    check(
        "repeated failures back off",
        next_interval(failing, outcome, False) > 300,
        next_interval(failing, outcome, False),
    )

    urgent = Watch(user_id=1, adaptive=True, interval_seconds=600, priority=3)
    check("priority polls sooner", next_interval(urgent, outcome, False) < cold)


def test_password_rules() -> None:
    print("\n== Password handling ==")
    from app.security import (
        create_access_token,
        decode_token,
        hash_password,
        password_strength_problem,
        verify_password,
    )

    hashed = hash_password("Figure-Hunt-2026")
    check("correct password verifies", verify_password("Figure-Hunt-2026", hashed))
    check("wrong password does not", not verify_password("figure-hunt-2026", hashed))
    check("empty hash is rejected", not verify_password("anything", ""))

    # bcrypt truncates at 72 bytes; long passphrases must keep full entropy.
    long_a = "a" * 100 + "1"
    long_b = "a" * 100 + "2"
    long_hash = hash_password(long_a)
    check("a long passphrase verifies", verify_password(long_a, long_hash))
    check("long passphrases are not truncated", not verify_password(long_b, long_hash))

    check("short passwords rejected", password_strength_problem("short") is not None)
    check("single-class rejected", password_strength_problem("aaaaaaaaaaaa") is not None)
    check("a good password passes", password_strength_problem("Figure-Hunt-2026") is None)

    token, expires = create_access_token(42, "token-id")
    payload = decode_token(token)
    check("token round-trips", payload is not None and payload["sub"] == "42")
    check("token carries its id", payload is not None and payload["jti"] == "token-id")
    check("token expires in the future", expires > datetime.now(timezone.utc))
    check("tampered tokens are rejected", decode_token(token + "x") is None)


def test_mfc_matching() -> None:
    print("\n== MyFigureCollection title matching ==")
    from app.services.enrich import title_confidence

    shop = "Senki Zessho Symphogear GX Tsubasa Kazanari 1/7 Complete Figure (Hobby Stock Exclusive)"
    good = "Senki Zesshou Symphogear GX - Kazanari Tsubasa - 1/7 (Hobby Stock, Wing)"
    bad = "One Piece - Roronoa Zoro - Acrylic Keychain"

    check("a real match scores high", title_confidence(shop, good) > 0.45, title_confidence(shop, good))
    check("an unrelated title scores low", title_confidence(shop, bad) < 0.2, title_confidence(shop, bad))
    check("empty input scores zero", title_confidence("", good) == 0.0)
    check("identical titles score 1", approx(title_confidence(good, good), 1.0))


def test_normalisation() -> None:
    print("\n== Shop payload normalisation ==")
    from app.providers.amiami import AmiAmiProvider

    provider = AmiAmiProvider()

    listing = provider._normalize_list_item(
        {
            "gcode": "FIGURE-153570-R",
            "gname": "Symphogear Tsubasa 1/7 Complete Figure",
            "min_price": 10380,
            "c_price_taxed": 29700,
            "maker_name": "Hobby Stock",
            "thumb_url": "/images/product/thumb300/232/FIGURE-153570.jpg",
            "condition_flg": 1,
            "instock_flg": 1,
            "releasedate": "2023-04-01 00:00:00",
            "jancode": "4589691204127",
        }
    )
    check("code carried over", listing.code == "FIGURE-153570-R")
    check("price read from min_price", listing.price == 10380)
    check("MSRP read from c_price_taxed", listing.list_price == 29700)
    check("pre-owned detected", listing.condition == "preowned")
    check("stock read from instock_flg", listing.in_stock is True)
    check("thumbnail made absolute", listing.image_url.startswith("https://img.amiami.com/"), listing.image_url)
    check("scale parsed from the name", listing.scale == "1/7")
    check("release date normalised", listing.release_date == "Apr-2023", listing.release_date)

    # The -R suffix alone marks a pre-owned listing.
    suffix_only = provider._normalize_list_item({"gcode": "FIG-MOE-6590-R", "gname": "x", "min_price": 1})
    check("the -R suffix implies pre-owned", suffix_only.condition == "preowned")

    detail = provider._normalize_detail(
        {
            "gcode": "FIGURE-207209",
            "gname": "Nendoroid Test",
            "sname": "(Pre-owned ITEM:B/BOX:B)Nendoroid Test",
            "price": 6210,
            "c_price_taxed": 6900,
            # soldout_flg is 1 on every detail response, including live
            # pre-orders, so it must not be treated as authoritative.
            "soldout_flg": 1,
            "stock": 1,
            "order_closed_flg": 0,
            "end_flg": 0,
            "cart_type": 8,
            "preorderitem": 1,
            "salestatus": "Pre-order",
            "releasedate": "Feb-2027",
        },
        {},
    )
    check("soldout_flg is ignored on detail", detail.order_closed is False)
    check("a pre-order is not in stock", detail.in_stock is False)
    check("a pre-order is flagged as one", detail.is_preorder is True)
    check("condition grade extracted", detail.condition_grade == "ITEM:B/BOX:B", detail.condition_grade)

    released = provider._normalize_detail(
        {
            "gcode": "FIGURE-1",
            "gname": "Released Thing",
            "price": 100,
            "soldout_flg": 1,
            "stock": 1,
            "order_closed_flg": 0,
            "end_flg": 0,
            "cart_type": 9,
            "preorderitem": 0,
            "salestatus": "Released",
        },
        {},
    )
    check("a released, orderable item is in stock", released.in_stock is True)

    closed = provider._normalize_detail(
        {"gcode": "FIGURE-2", "gname": "Closed", "price": 100, "stock": 1, "order_closed_flg": 1},
        {},
    )
    check("order_closed_flg is respected", closed.order_closed is True and closed.in_stock is False)


def test_local_filters() -> None:
    print("\n== Local filtering and sorting ==")
    from app.providers.amiami import AmiAmiProvider
    from app.providers.base import NormalizedItem, SearchQuery

    def make(code: str, price: float, condition: str = "new", in_stock: bool = True) -> NormalizedItem:
        return NormalizedItem(
            provider="amiami",
            code=code,
            name=f"{code} acrylic keychain" if code == "C" else code,
            url="",
            price=price,
            condition=condition,
            in_stock=in_stock,
        )

    items = [make("A", 100), make("B", 300, "preowned"), make("C", 200), make("D", 50, in_stock=False)]

    only_preowned = AmiAmiProvider._apply_local_filters(items, SearchQuery(condition="preowned"))
    check("condition filter", [i.code for i in only_preowned] == ["B"])

    in_stock = AmiAmiProvider._apply_local_filters(items, SearchQuery(stock_filter="in_stock"))
    check("stock filter", [i.code for i in in_stock] == ["A", "B", "C"])

    priced = AmiAmiProvider._apply_local_filters(items, SearchQuery(min_price=100, max_price=250))
    check("price range filter", [i.code for i in priced] == ["A", "C"])

    excluded = AmiAmiProvider._apply_local_filters(items, SearchQuery(exclude_keywords=["keychain"]))
    check("exclusion filter", "C" not in [i.code for i in excluded])

    ascending = AmiAmiProvider._apply_local_sort(items, "price_asc")
    check("price ascending", [i.price for i in ascending] == [50, 100, 200, 300])
    descending = AmiAmiProvider._apply_local_sort(items, "price_desc")
    check("price descending", [i.price for i in descending] == [300, 200, 100, 50])


def test_rate_limiting() -> None:
    print("\n== Rate limiter and circuit breaker ==")
    import time

    from app.providers.ratelimit import CircuitBreaker, CircuitOpen, RateLimitExceeded, TokenBucket

    bucket = TokenBucket(rate_per_minute=60, burst=2)
    start = time.monotonic()
    bucket.acquire()
    bucket.acquire()
    check("burst tokens are immediate", time.monotonic() - start < 0.2)

    tiny = TokenBucket(rate_per_minute=1, burst=1)
    tiny.acquire()
    try:
        tiny.acquire(timeout=0.5)
        check("an exhausted bucket blocks", False, "no exception raised")
    except RateLimitExceeded:
        check("an exhausted bucket blocks", True)

    breaker = CircuitBreaker(threshold=3, reset_after=60.0)
    check("a fresh breaker is closed", not breaker.is_open)
    for _ in range(2):
        breaker.record_failure()
    check("it tolerates failures below the threshold", not breaker.is_open)
    breaker.record_failure()
    check("it opens at the threshold", breaker.is_open)
    try:
        breaker.check()
        check("an open breaker refuses calls", False, "no exception raised")
    except CircuitOpen:
        check("an open breaker refuses calls", True)
    breaker.record_success()
    check("success closes it again", not breaker.is_open)


def test_pacing() -> None:
    print()
    print("== Human-like request pacing ==")
    import statistics
    from datetime import datetime as dt

    from app.services.pacing import HumanPacer

    daytime = dt(2026, 6, 1, 14, 0)
    pacer = HumanPacer(requests_per_minute=12)
    # Seeded so the assertions below are about the distribution's shape rather
    # than about whichever sequence today's entropy happened to produce.
    pacer._rng.seed(20260601)
    delays = [pacer.next_delay(daytime) for _ in range(6000)]

    achieved = 60 / statistics.mean(delays)
    check("the configured rate is actually achieved", 10.5 <= achieved <= 13.5, achieved)
    check("no delay falls below the floor", min(delays) >= pacer.minimum_delay)

    # The whole point is that the gaps are irregular.
    spread = statistics.pstdev(delays) / statistics.mean(delays)
    check("gaps vary rather than tick", spread > 0.5, spread)
    # A metronome would yield exactly one distinct value; anything in this
    # range is unmistakably irregular. The bar is deliberately far from the
    # observed count so the test cannot start failing on a different draw.
    distinct = len({round(d, 1) for d in delays[:300]})
    check("gaps are not a fixed tick", distinct > 40, distinct)
    repeats = sum(1 for a, b in zip(delays, delays[1:]) if abs(a - b) < 0.01)
    check("consecutive gaps almost never repeat", repeats < len(delays) * 0.02, repeats)
    check("long breaks do occur", max(delays) > statistics.mean(delays) * 4, max(delays))

    # And it eases off overnight.
    pacer._rng.seed(20260602)
    night = [pacer.next_delay(dt(2026, 6, 1, 3, 0)) for _ in range(2000)]
    check(
        "the small hours are slower",
        statistics.mean(night) > statistics.mean(delays) * 1.5,
        (statistics.mean(night), statistics.mean(delays)),
    )
    check("daytime is not slowed", not HumanPacer()._diurnal_factor(daytime) > 1.0)


def test_crawler_cycles() -> None:
    print()
    print("== Catalogue crawl scheduling ==")
    from datetime import datetime as dt
    from datetime import timedelta as td

    from app.models import CatalogCrawl
    from app.services.crawler import _eta_seconds, _page_limit

    fresh = CatalogCrawl(provider="amiami", scope="s", pages_total=0)
    check("an unmeasured slice is unbounded", _page_limit(fresh) > 1000)

    first = CatalogCrawl(provider="amiami", scope="s", pages_total=200, cycles_completed=0)
    check("the first pass reads everything", _page_limit(first) == 200)

    later = CatalogCrawl(
        provider="amiami",
        scope="s",
        pages_total=200,
        cycles_completed=1,
        head_pages=20,
        last_full_sweep_at=datetime.now(timezone.utc),
    )
    check("later passes only re-read the newest pages", _page_limit(later) == 20, _page_limit(later))

    stale = CatalogCrawl(
        provider="amiami",
        scope="s",
        pages_total=200,
        cycles_completed=5,
        head_pages=20,
        full_sweep_interval_days=7,
        last_full_sweep_at=datetime.now(timezone.utc) - td(days=8),
    )
    check("a full sweep comes round again", _page_limit(stale) == 200, _page_limit(stale))

    check("nothing left means no estimate", _eta_seconds(0) is None)
    check("an estimate is produced when work remains", (_eta_seconds(100) or 0) > 0)
    check(
        "more pages take longer",
        (_eta_seconds(200) or 0) > (_eta_seconds(100) or 0),
    )


def test_crawler_yields_to_watches() -> None:
    print()
    print("== The crawl stands aside for watches ==")
    from app.db import SessionLocal, init_db
    from app.models import User, Watch
    from app.security import hash_password
    from app.services.crawler import ensure_scopes, watches_are_due

    init_db()
    db = SessionLocal()
    try:
        check("nothing due on an empty instance", not watches_are_due(db))

        user = User(
            email="crawl@example.com",
            username="crawluser",
            password_hash=hash_password("Crawler-Test-2026"),
        )
        db.add(user)
        db.commit()

        # A watch that has never run is due immediately.
        watch = Watch(user_id=user.id, query="test", enabled=True, next_run_at=None)
        db.add(watch)
        db.commit()
        check("a never-run watch counts as due", watches_are_due(db))

        watch.next_run_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db.commit()
        check("a scheduled watch does not", not watches_are_due(db))

        watch.enabled = False
        watch.next_run_at = None
        db.commit()
        check("a paused watch never blocks the crawl", not watches_are_due(db))

        created = ensure_scopes(db)
        check("default slices are registered", created >= 4, created)
        check("registering twice adds nothing", ensure_scopes(db) == 0)
    finally:
        db.close()


def test_mfc_cookie_parsing() -> None:
    print()
    print("== MyFigureCollection session cookies ==")
    from app.enrichment.mfc import MfcClient

    parse = MfcClient.parse_cookies

    check("a bare value is taken as the session", parse("abc123") == {"PHPSESSID": "abc123"})
    check("a named pair works", parse("PHPSESSID=abc123") == {"PHPSESSID": "abc123"})
    check(
        "a whole cookie header works",
        parse("PHPSESSID=abc; addtl_consent=2~20; rzr_seg=z")
        == {"PHPSESSID": "abc", "addtl_consent": "2~20", "rzr_seg": "z"},
    )
    check("whitespace and stray semicolons survive", parse("  PHPSESSID = abc ;; ") == {"PHPSESSID": "abc"})
    check("quotes are stripped", parse('PHPSESSID="abc"') == {"PHPSESSID": "abc"})
    check("empty input yields nothing", parse("") == {} and parse(None) == {})

    # The consent cookie sits next to the session in every cookie manager and
    # is the one people copy by mistake, so it must be called out rather than
    # silently accepted as a login.
    consent_only = parse("addtl_consent=2~20.43; euconsent-v2=CP")
    check("consent cookies parse but carry no session", "PHPSESSID" not in consent_only)
    check("and are still kept", len(consent_only) == 2, consent_only)


def test_health_detection() -> None:
    print()
    print("== Noticing when the machinery breaks ==")
    from app.db import SessionLocal, init_db
    from app.models import (
        CatalogCrawl,
        ChannelType,
        NotificationChannel,
        User,
        UserRole,
        Watch,
    )
    from app.security import hash_password
    from app.services import health

    init_db()
    db = SessionLocal()
    try:
        admin = User(
            email="health-admin@example.com",
            username="healthadmin",
            password_hash=hash_password("Health-Admin-2026"),
            role=UserRole.admin,
        )
        db.add(admin)
        db.commit()

        check("a quiet instance reports nothing", not health.collect_issues(db))

        # A channel that keeps rejecting messages is the worst failure there
        # is: alerts are raised and then thrown away.
        channel = NotificationChannel(
            user_id=admin.id,
            type=ChannelType.telegram,
            name="Phone",
            config={"bot_token": "x", "chat_id": "1"},
            failure_count=7,
            last_error="401 Unauthorized",
        )
        db.add(channel)
        db.commit()
        keys = {i.key for i in health.collect_issues(db)}
        check("a failing channel is noticed", f"channel:{channel.id}" in keys, keys)

        issue = next(i for i in health.collect_issues(db) if i.key.startswith("channel:"))
        check("the alert is marked urgent", issue.urgent)
        check("and carries the upstream error", "401" in issue.detail, issue.detail)

        # Watches that fail repeatedly.
        watch = Watch(user_id=admin.id, query="broken", enabled=True, consecutive_errors=9)
        db.add(watch)
        db.commit()
        keys = {i.key for i in health.collect_issues(db)}
        check("repeatedly failing watches are noticed", "watches:failing" in keys, keys)

        # A stalled catalogue slice.
        db.add(
            CatalogCrawl(
                provider="amiami",
                scope="figures_test",
                label="Test slice",
                enabled=True,
                consecutive_errors=6,
                last_error="upstream returned 403",
            )
        )
        db.commit()
        keys = {i.key for i in health.collect_issues(db)}
        check("a stalled crawl slice is noticed", "crawler:stalled" in keys, keys)

        # Reporting must be once-per-problem, not once-per-check, or people
        # learn to ignore it.
        first = health.check(db, notify_enabled=False)
        check("the report lists every problem", len(first["issues"]) >= 3, first["issues"])
        check("the instance is not called healthy", not first["healthy"])

        # Clear them and confirm the resolution is tracked.
        db.delete(channel)
        watch.consecutive_errors = 0
        for crawl in db.query(CatalogCrawl).all():
            crawl.consecutive_errors = 0
        db.commit()

        second = health.check(db, notify_enabled=False)
        check("clearing the faults clears the report", second["healthy"], second["issues"])
        check("and the resolution is recorded", len(second["resolved"]) >= 3, second["resolved"])

        third = health.check(db, notify_enabled=False)
        check("nothing is resolved twice", third["resolved"] == [], third["resolved"])
    finally:
        db.close()


def test_image_url_derivation() -> None:
    print()
    print("== Product photo URLs ==")
    from app.services import images

    main = "https://img.amiami.com/images/product/main/104/FIG-MOE-2210.jpg"
    thumb = "https://img.amiami.com/images/product/thumb300/104/FIG-MOE-2210.jpg"

    # Which size an item carries depends purely on where it was seen, so both
    # directions have to work or grids pull 80 KB tiles.
    check("a full image yields its thumbnail", images.thumbnail_of(main) == thumb)
    check("a thumbnail yields its full image", images.full_of(thumb) == main)
    check("a thumbnail is already a thumbnail", images.thumbnail_of(thumb) == thumb)
    check("a full image is already full", images.full_of(main) == main)

    other = "https://example.com/photo.jpg"
    check("an unrecognised path has no thumbnail", images.thumbnail_of(other) is None)

    check("size is read from the path", images.kind_for(main) == "main")
    check("and for thumbnails too", images.kind_for(thumb) == "thumb")

    # Content addressing: same URL, same key; different URL, different key.
    check("keys are stable", images.key_for(main) == images.key_for(main))
    check("keys are distinct", images.key_for(main) != images.key_for(thumb))
    check("keys are short and safe", len(images.key_for(main)) == 32)

    check(
        "the public route points at this instance",
        images.public_url(main) == "/api/images/" + images.key_for(main),
    )
    check("a missing URL stays missing", images.public_url(None) is None)

    # Sharded storage, so no directory ends up with a hundred thousand files.
    path = images.path_for(images.key_for(main), "image/jpeg")
    check("files are sharded into subdirectories", len(path.relative_to(images.cache_root()).parts) == 3, path)
    check("the extension follows the content type", path.suffix == ".jpg")
    check(
        "a png is stored as a png",
        images.path_for("abc", "image/png").suffix == ".png",
    )


def test_image_selection() -> None:
    print()
    print("== Which photos are worth keeping ==")
    from app.models import Item
    from app.services import images

    main = "https://img.amiami.com/images/product/main/104/X.jpg"
    thumb = "https://img.amiami.com/images/product/thumb300/104/X.jpg"

    # An item seen only in search results carries a thumbnail; one fetched in
    # detail carries the full image. Either way both sizes should be kept.
    from_search = Item(provider="amiami", code="X", name="X", image_url=thumb, images=[])
    from_detail = Item(provider="amiami", code="X", name="X", image_url=main, images=[main])

    for label, item in (("from search", from_search), ("from detail", from_detail)):
        urls = images.urls_for_item(item, include_full=True)
        check(f"{label}: both sizes selected", set(urls) == {thumb, main}, urls)
        check(f"{label}: thumbnail comes first", urls[0] == thumb, urls)

    lean = images.urls_for_item(from_detail, include_full=False)
    check("thumbnails-only mode keeps just the small one", lean == [thumb], lean)

    empty = Item(provider="amiami", code="Y", name="Y", image_url=None, images=[])
    check("an item with no photo selects nothing", images.urls_for_item(empty) == [])


def test_settings() -> None:
    print("\n== Configuration ==")
    from app.config import Settings

    sqlite = Settings(data_dir=TMP, database_url="")
    check("SQLite is the default", sqlite.is_sqlite and sqlite.resolved_database_url.startswith("sqlite:///"))

    bare = Settings(data_dir=TMP, database_url="postgres://u:p@h/db")
    check(
        "bare postgres:// is upgraded to a driver URL",
        bare.resolved_database_url.startswith("postgresql+psycopg://"),
        bare.resolved_database_url,
    )
    check("postgres is not sqlite", not bare.is_sqlite)


def main() -> int:
    test_url_parsing()
    test_release_dates()
    test_landed_cost()
    test_weight_estimation()
    test_shipping_table()
    test_trigger_rules()
    test_trigger_switches()
    test_grade_parsing()
    test_variant_selection()
    test_grade_filter_blocks_alerts()
    test_quiet_hours()
    test_adaptive_intervals()
    test_password_rules()
    test_mfc_matching()
    test_normalisation()
    test_local_filters()
    test_rate_limiting()
    test_pacing()
    test_crawler_cycles()
    test_crawler_yields_to_watches()
    test_mfc_cookie_parsing()
    test_health_detection()
    test_image_url_derivation()
    test_image_selection()
    test_settings()

    print(f"\n{'=' * 46}\n  {PASS} passed, {FAIL} failed\n{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
