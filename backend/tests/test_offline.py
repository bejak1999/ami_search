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

    check("nendoroid is light", landed_cost.estimate_weight(nendoroid, profile) == 250)
    check("1/7 scale is heavy", landed_cost.estimate_weight(scale_figure, profile) == 850)
    check("keychain is lightest", landed_cost.estimate_weight(keychain, profile) == 90)
    check("unknown falls back to the default", landed_cost.estimate_weight(unknown, profile) == 800)

    # An explicit height in the spec sheet should beat any keyword guess.
    sized = Item(name="Something", provider="amiami", code="E", spec="Scale: 1/7\nSize: H260mm")
    check("spec height wins", approx(landed_cost.estimate_weight(sized, profile), 900, 80))

    tall = Item(name="Something", provider="amiami", code="F", spec="Size: H400mm")
    check(
        "a taller figure weighs more",
        landed_cost.estimate_weight(tall, profile) > landed_cost.estimate_weight(sized, profile),
    )


def test_weights_match_real_parcels() -> None:
    print("\n== Weights agree with parcels actually paid for ==")
    from app.models import Item
    from app.services import landed_cost, shipping_rates

    # Three shipments actually paid for, decoded back through AmiAmi's own
    # small-packet chart for zone three. All were scale figures between 1/6
    # and 1/7, which is the size the estimate is most often asked about, and
    # they bracket the answer at 1000-1500 g including the shop's packaging.
    #   JPY 2310 unregistered  -> 1001-1100 g
    #   JPY 3130 registered    -> 1201-1300 g
    #   JPY 3490 registered    -> 1401-1500 g
    OBSERVED_LOW, OBSERVED_HIGH = 1000, 1500

    profile = landed_cost.default_profile(user_id=1)
    for name, spec in (
        ("Something 1/7 Complete Figure", None),
        ("Something 1/6 Complete Figure", None),
        ("Scale Figure", "Size: Approx. H25cm"),
        ("Scale Figure", "Size: Approx. H28cm"),
    ):
        item = Item(provider="amiami", code="X", name=name, spec=spec)
        weight = landed_cost.shipment_weight(item, profile)
        check(
            f"{name if spec is None else spec} lands in the observed range",
            OBSERVED_LOW <= weight <= OBSERVED_HIGH,
            f"{weight} g",
        )

    # And the postage that follows from it is what actually got paid.
    seventh = Item(provider="amiami", code="X", name="Something 1/7 Complete Figure")
    quoted = shipping_rates.lookup(
        "zone3", "small_packet", landed_cost.shipment_weight(seventh, profile)
    )
    check("a 1/7 figure quotes the fare that was paid", quoted == 2310, quoted)

    # The old table put the same figure two brackets higher, which is the
    # regression this test exists to catch.
    check("and not the old over-estimate", quoted < 3570, quoted)


def test_shipping_table() -> None:
    print("\n== Shipping brackets ==")
    from app.services import landed_cost

    profile = landed_cost.default_profile(user_id=1)
    profile.shipping_mode = "table"
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


def test_amiami_shipping_rates() -> None:
    print("\n== AmiAmi's published rate charts ==")
    from app.services import landed_cost, shipping_rates

    # Spot checks against the corners of the shop's own tables. If AmiAmi
    # reprices, these are the assertions that should fail first.
    check("zone 3, 300 g unregistered", shipping_rates.lookup("zone3", "small_packet", 300) == 870)
    check("zone 3, 300 g registered", shipping_rates.lookup("zone3", "small_packet_registered", 300) == 1330)
    check("zone 4, 2 kg unregistered", shipping_rates.lookup("zone4", "small_packet", 2000) == 4820)
    check("zone 1, 500 g EMS", shipping_rates.lookup("zone1", "ems", 500) == 1450)
    check("zone 5, 30 kg EMS", shipping_rates.lookup("zone5", "ems", 30000) == 77700)
    check("zone 2, 30 kg surface", shipping_rates.lookup("zone2", "surface_parcel", 30000) == 14600)

    check("every zone is charted", set(shipping_rates.RATES) == set(shipping_rates.ZONES))
    check(
        "every zone quotes every service",
        all(set(services) == set(shipping_rates.SERVICES) for services in shipping_rates.RATES.values()),
    )
    check(
        "brackets ascend in weight and price",
        all(
            [w for w, _ in steps] == sorted(w for w, _ in steps)
            and [c for _, c in steps] == sorted(c for _, c in steps)
            for zone in shipping_rates.RATES.values()
            for steps in zone.values()
        ),
    )

    # A bracket is an upper bound, so anything under it pays the same.
    check("199 g pays the 300 g rate", shipping_rates.lookup("zone3", "small_packet", 199) == 870)
    check("301 g moves up a bracket", shipping_rates.lookup("zone3", "small_packet", 301) == 1050)

    check("small packet stops at 2 kg", shipping_rates.lookup("zone3", "small_packet", 2001) is None)
    check("nothing carries 31 kg", shipping_rates.cheapest("zone3", 31000) is None)
    # Air parcel's chart simply starts at 1 kg, so a lighter parcel pays the
    # smallest rate listed rather than nothing at all.
    check(
        "a light air parcel pays the minimum",
        shipping_rates.lookup("zone3", "air_parcel", 300) == shipping_rates.lookup("zone3", "air_parcel", 1000),
    )

    check("Germany is the third zone", shipping_rates.zone_for_country("DE") == "zone3")
    check("the US has its own", shipping_rates.zone_for_country("us") == "zone4")
    check("Taiwan is the first", shipping_rates.zone_for_country("TW") == "zone1")
    check("Brazil is the fifth", shipping_rates.zone_for_country("BR") == "zone5")
    check("Canada is not the US", shipping_rates.zone_for_country("CA") == "zone3")
    check("an unknown country falls back", shipping_rates.zone_for_country("??") == "zone3")
    check("so does a missing one", shipping_rates.zone_for_country(None) == "zone3")

    cheap = shipping_rates.cheapest("zone3", 1200)
    check("auto picks the cheapest carrier", cheap == ("small_packet", 2490), cheap)
    over = shipping_rates.cheapest("zone3", 2100)
    check("and falls through once small packet is out", over[0] == "surface_parcel", over)

    # Sea mail undercuts everything at weight, but it takes months, so the
    # air-only mode has to refuse it and quote something that flies.
    air = shipping_rates.cheapest("zone3", 2100, air_only=True)
    check("air-only skips sea mail", air[0] not in shipping_rates.SURFACE_SERVICES, air)
    check("and costs more for it", air[1] > over[1], (air, over))
    check(
        "air-only still gives up past 30 kg",
        shipping_rates.cheapest("zone3", 31000, air_only=True) is None,
    )

    profile = landed_cost.default_profile(user_id=1)
    check("new profiles use the real charts", profile.shipping_mode == "amiami")
    check("and fly by default", profile.shipping_service == "auto_air")

    # Forcing a service that cannot take the weight must not silently drop
    # the buyer onto a boat.
    profile.shipping_service = "small_packet_registered"
    profile.packaging_grams = 900
    quote = landed_cost._amiami_shipping(2100, profile, None, "JPY")
    check("no FX rate means no chart quote", quote is None)

    # Without a database there is no yen rate, so the estimator has to fall
    # back rather than quote a number it cannot convert.
    cost, note = landed_cost.shipping_cost(1600, profile)
    check("no FX rate falls back to the table", cost == 30.0, (cost, note))
    check("and the note says the number is estimated", note.startswith("Estimated:"), note)


def test_packaging_weight() -> None:
    print("\n== Packaging rides along on every parcel ==")
    from app.models import Item
    from app.services import landed_cost

    profile = landed_cost.default_profile(user_id=1)
    figure = Item(name="Honolulu 1/7 Complete Figure", scale="1/7", category="Figure")

    check("the default allowance is 250 g", profile.packaging_grams == 250)
    check("goods weigh what they weigh", landed_cost.estimate_weight(figure, profile) == 850)
    check("the parcel weighs more", landed_cost.shipment_weight(figure, profile) == 1100)
    check(
        "packaging is counted once, not per unit",
        landed_cost.shipment_weight(figure, profile, quantity=3) == 850 * 3 + 250,
    )

    profile.packaging_grams = 0
    check("it can be switched off", landed_cost.shipment_weight(figure, profile) == 850)
    profile.packaging_grams = None
    check("a null column still packs the box", landed_cost.shipment_weight(figure, profile) == 1100)
    profile.packaging_grams = 250

    profile.weight_scale = 1.4
    check(
        "the dial moves the whole estimate",
        landed_cost.shipment_weight(figure, profile) == int(850 * 1.4) + 250,
    )
    profile.weight_scale = 1.0

    # The point of the allowance: it can push a parcel out of a bracket.
    light = Item(name="Nendoroid Something", category="Figure")
    from app.services import shipping_rates

    bare = shipping_rates.lookup("zone3", "small_packet", landed_cost.estimate_weight(light, profile))
    packed = shipping_rates.lookup("zone3", "small_packet", landed_cost.shipment_weight(light, profile))
    check("a 400 g nendoroid ships as an 800 g parcel", packed > bare, (bare, packed))


def test_shelf_sequences() -> None:
    print("\n== Copy codes carry an intake counter ==")
    from app.services import shelflife

    check("a copy code yields its number", shelflife.sequence_of("FIGURE-140238-R459") == 459)
    check("lower case too", shelflife.sequence_of("figure-140238-r7") == 7)
    check("the bare product code is not a copy", shelflife.sequence_of("FIGURE-140238-R") is None)
    check("nor is a first-hand code", shelflife.sequence_of("FIG-MOE-6590") is None)
    check("nor an empty one", shelflife.sequence_of("") is None)
    check("nor a missing one", shelflife.sequence_of(None) is None)
    check("whitespace is tolerated", shelflife.sequence_of("  FIGURE-1-R12  ") == 12)


def test_kaplan_meier() -> None:
    print("\n== The median survives copies that have not sold yet ==")
    from app.services.shelflife import kaplan_meier_median as km

    check("no data, no median", km([]) is None)
    check("four completed lifetimes", km([(2, True), (4, True), (6, True), (8, True)]) == 4)
    check(
        "still-listed copies do not drag it down",
        km([(2, True), (4, True), (6, False), (8, False)]) == 4,
    )
    check(
        "mostly unfinished means no median yet",
        km([(2, True), (4, False), (6, False), (8, False)]) is None,
    )
    check("nothing finished at all", km([(3, False), (9, False)]) is None)

    # The trap the estimator exists to avoid: two fast sales and eight slow
    # copies still sitting there. Averaging only what sold says 3 days.
    biased = [(3.0, True), (3.0, True)] + [(60.0, False)] * 8
    naive = sum(d for d, done in biased if done) / 2
    check("naive average of completed sales is 3 days", abs(naive - 3.0) < 0.01)
    check("the estimator refuses that answer", km(biased) is None, km(biased))


def _purge(db, *codes: str) -> None:
    """Remove what a shelf test created.

    Every suite here shares one database, so rows left lying about quietly
    change the totals another suite asserts on. Deleting by item cascades to
    the copies and price points hanging off it.
    """
    from app.models import Item

    for code in codes:
        for row in db.query(Item).filter(Item.code == code).all():
            db.delete(row)
    db.commit()


def _shelf_item(db, code: str = "FIGURE-140238-R"):
    from app.models import Condition, Item

    item = Item(
        provider="amiami",
        code=code,
        name="Test Figure",
        currency="JPY",
        condition=Condition.preowned,
    )
    db.add(item)
    db.commit()
    return item


def _copy(seq: int, price: float, grade: str = "B") -> dict:
    return {
        "code": "FIGURE-140238-R" + str(seq),
        "price": price,
        "condition": "Condition Item:" + grade + " Box:B",
        "item_grade": grade,
        "box_grade": "B",
    }


def _observe(db, item, day: int, copies: list, base: datetime) -> None:
    """One detail fetch, at a controlled point in time."""
    from app.services import shelflife

    when = base + timedelta(days=day)
    item.prev_detail_fetch_at = item.last_detail_fetch_at
    item.last_detail_fetch_at = when
    shelflife.reconcile(db, item, copies, observed_at=when, commit=True)


def test_shelf_reconciliation() -> None:
    print("\n== Copies appearing and vanishing ==")
    from app.db import SessionLocal, init_db
    from app.models import ListingOutcome, ListingStatus
    from app.services import shelflife

    init_db()
    db = SessionLocal()
    base = datetime(2026, 6, 1, tzinfo=timezone.utc)
    item = _shelf_item(db, "FIGURE-140238-R")

    _observe(db, item, 0, [_copy(100, 12240), _copy(101, 13770, "A")], base)
    check("first look records both copies", len(item.listings) == 2)
    first = {row.code: row for row in item.listings}
    check(
        "neither has a known start",
        all(row.appeared_after is None for row in item.listings),
    )

    _observe(db, item, 3, [_copy(100, 12240), _copy(101, 13770, "A"), _copy(102, 14500)], base)
    third = next(row for row in item.listings if row.sequence == 102)
    check("a new copy is picked up", third is not None)
    check(
        "and its start is bracketed by the previous look",
        third.appeared_after == base,
        third.appeared_after,
    )

    _observe(db, item, 5, [_copy(100, 12240), _copy(102, 14500)], base)
    gone = first["FIGURE-140238-R101"]
    check("the missing copy is closed out", gone.status == ListingStatus.gone)
    check("and read as a sale", gone.outcome == ListingOutcome.sold)
    life = shelflife.lifetime_of(gone)
    check("we witnessed three days of it", abs(life.certain_days - 3.0) < 0.01, life)
    check("its start is unknown, so no ceiling", life.max_days is None)
    check("and the UI is told to say 'at least'", life.open_start)

    bounded = shelflife.lifetime_of(third)
    check("a bracketed copy is still listed", bounded.open_end)

    _observe(db, item, 9, [_copy(100, 11900), _copy(102, 14500)], base)
    hundred = first["FIGURE-140238-R100"]
    check("a markdown is recorded", hundred.last_price == 11900)
    check("the original asking price survives", hundred.price == 12240)
    check(
        "and the cut is in the price history",
        any(p.price == 11900 for p in hundred.prices),
    )

    _observe(db, item, 12, [_copy(100, 11900), _copy(102, 14500), _copy(101, 13770, "A")], base)
    check("a returned copy reuses its row", len(item.listings) == 3)
    check("and is live again", gone.status == ListingStatus.live)
    check("its clock restarts rather than spanning the gap", gone.vanished_before is None)
    check(
        "the fresh spell starts where it reappeared",
        abs((shelflife.lifetime_of(gone, base + timedelta(days=12)).certain_days)) < 0.01,
    )

    _purge(db, "FIGURE-140238-R")
    db.close()


def test_copy_claimed_by_another_product() -> None:
    print("\n== The same copy turning up under a different product ==")
    from app.db import SessionLocal, init_db
    from app.models import Listing
    from app.services import shelflife

    init_db()
    db = SessionLocal()
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    first = _shelf_item(db, "FIGURE-555001-R")
    second = _shelf_item(db, "FIGURE-555002-R")

    shared = _copy(11, 3000)
    shared["code"] = "FIGURE-555001-R011"

    _observe(db, first, 0, [shared], base)
    check("the copy lands on the first product", db.query(Listing).filter(
        Listing.code == "FIGURE-555001-R011").one().item_id == first.id)

    # The shop now serves the very same copy code under another product. A
    # second row is impossible - provider plus code is unique - so this used
    # to be an integrity error that took the whole poll down.
    crashed = False
    try:
        _observe(db, second, 1, [shared], base)
    except Exception as exc:  # noqa: BLE001
        crashed = True
        print(f"         raised {type(exc).__name__}: {str(exc)[:70]}")
    check("reconciling does not blow up", not crashed)

    if not crashed:
        row = db.query(Listing).filter(Listing.code == "FIGURE-555001-R011").one()
        check("the copy follows the shop to the new product", row.item_id == second.id)
        check("and there is still only one of it",
              db.query(Listing).filter(Listing.code == "FIGURE-555001-R011").count() == 1)

    _purge(db, "FIGURE-555001-R", "FIGURE-555002-R")
    db.close()


def test_shelf_wholesale_disappearance() -> None:
    print("\n== A shelf that empties all at once ==")
    from app.db import SessionLocal, init_db
    from app.models import ListingOutcome, ListingStatus
    from app.services import shelflife

    init_db()
    db = SessionLocal()
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    item = _shelf_item(db, "FIGURE-999001-R")

    three = [_copy(10, 5000), _copy(11, 5200), _copy(12, 5400)]
    for row in three:
        row["code"] = row["code"].replace("140238", "999001")
    _observe(db, item, 0, three, base)
    _observe(db, item, 1, three[:1], base)
    survivors = [r for r in item.listings if r.status == ListingStatus.live]
    check("two copies going is read as two sales", len(survivors) == 1)
    check(
        "each one individually",
        all(
            r.outcome == ListingOutcome.sold
            for r in item.listings
            if r.status == ListingStatus.gone
        ),
    )

    # Now empty the whole shelf in one step, which is a weaker claim.
    item2 = _shelf_item(db, "FIGURE-999002-R")
    four = []
    for seq, price in ((20, 5000), (21, 5200), (22, 5400), (23, 5600)):
        row = _copy(seq, price)
        row["code"] = row["code"].replace("140238", "999002")
        four.append(row)
    _observe(db, item2, 0, four, base)
    _observe(db, item2, 1, [], base)
    check(
        "an empty response is not treated as four sales",
        all(r.status == ListingStatus.live for r in item2.listings),
        [r.status for r in item2.listings],
    )

    _observe(db, item2, 2, four[:1], base)
    check(
        "three of four going is read as three sales",
        all(
            r.outcome == ListingOutcome.sold
            for r in item2.listings
            if r.status == ListingStatus.gone
        ),
    )

    # A shelf that turns over completely between two looks is a different
    # claim: four simultaneous sales and a stock rotation look identical, so
    # the weaker reading wins.
    item3 = _shelf_item(db, "FIGURE-999003-R")
    old_stock = []
    for seq, price in ((20, 5000), (21, 5200), (22, 5400), (23, 5600)):
        row = _copy(seq, price)
        row["code"] = row["code"].replace("140238", "999003")
        old_stock.append(row)
    replacement = _copy(30, 5800)
    replacement["code"] = replacement["code"].replace("140238", "999003")
    _observe(db, item3, 0, old_stock, base)
    _observe(db, item3, 1, [replacement], base)
    withdrawn = [r for r in item3.listings if r.status == ListingStatus.gone]
    check("a whole shelf replaced at once is flagged", len(withdrawn) == 4)
    check(
        "as possibly withdrawn rather than sold",
        all(r.outcome == ListingOutcome.withdrawn for r in withdrawn),
        [r.outcome for r in withdrawn],
    )
    check(
        "and a withdrawal is not evidence of a sale",
        item3.dwell_samples == 0,
        item3.dwell_samples,
    )

    _purge(db, "FIGURE-999001-R", "FIGURE-999002-R", "FIGURE-999003-R")
    db.close()


def test_shelf_intake_estimate() -> None:
    print("\n== Little's Law from the intake counter ==")
    from app.db import SessionLocal, init_db
    from app.services import shelflife

    init_db()
    db = SessionLocal()
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    item = _shelf_item(db, "FIGURE-777001-R")

    def copies(seqs):
        out = []
        for seq in seqs:
            row = _copy(seq, 9000)
            row["code"] = "FIGURE-777001-R" + str(seq)
            out.append(row)
        return out

    _observe(db, item, 0, copies([200, 201, 202]), base)
    rate, basis = shelflife.intake_rate(item)
    check("one look cannot measure a rate", basis != "measured", basis)

    _observe(db, item, 20, copies([210, 211, 212]), base)
    rate, basis = shelflife.intake_rate(item)
    check("two looks twenty days apart can", basis == "measured", basis)
    check("ten copies over twenty days is half a day each", abs(rate - 0.5) < 0.01, rate)

    days, _ = shelflife.dwell_from_intake(item)
    check("three on the shelf at that rate is six days", abs(days - 6.0) < 0.5, days)
    check("which becomes the cached figure", item.dwell_basis == "intake")

    # The highest live number falls when the newest copy sells. The counter
    # itself has not gone backwards, and the rate must not either.
    _observe(db, item, 25, copies([210]), base)
    check("a sold top copy does not rewind the counter", item.intake_last_seq == 212)
    rate_after, _ = shelflife.intake_rate(item)
    check("so the rate stays positive", rate_after > 0, rate_after)

    _purge(db, "FIGURE-777001-R")
    db.close()


def test_shelf_tiers() -> None:
    print("\n== Where the request budget goes ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item, User, UserRole, Watch, WatchKind
    from app.services import shelfwatch

    init_db()
    db = SessionLocal()
    user = User(username="tiers", email="tiers@example.com", password_hash="x", role=UserRole.user)
    db.add(user)
    db.commit()

    plain = Item(provider="amiami", code="FIGURE-800001-R", name="Cold", condition=Condition.preowned)
    watched = Item(provider="amiami", code="FIGURE-800002-R", name="Hot", condition=Condition.preowned)
    busy = Item(provider="amiami", code="FIGURE-800003-R", name="Warm", condition=Condition.preowned)
    busy.listing_count = 4
    db.add_all([plain, watched, busy])
    db.commit()
    db.add(
        Watch(
            user_id=user.id,
            kind=WatchKind.item,
            label="Hot one",
            provider="amiami",
            item_code="FIGURE-800002-R",
            enabled=True,
        )
    )
    db.commit()

    check("an untouched product is cold", shelfwatch.tier_for(db, plain) == shelfwatch.COLD)
    check("one someone watches is hot", shelfwatch.tier_for(db, watched) == shelfwatch.HOT)

    # A watch aimed at one copy is really aimed at that product's shelf, so
    # the product still has to come out hot.
    db.add(
        Watch(
            user_id=user.id,
            kind=WatchKind.item,
            label="One copy",
            provider="amiami",
            item_code="FIGURE-800001-R044",
            enabled=True,
        )
    )
    db.commit()
    check(
        "a watch on a single copy still heats its product",
        shelfwatch.tier_for(db, plain) == shelfwatch.HOT,
    )
    check("one with a moving shelf is warm", shelfwatch.tier_for(db, busy) == shelfwatch.WARM)

    # Other suites share this database, so assert on these three rather than
    # on a total that depends on what ran before.
    due = {i.code for i in shelfwatch.due_items(db, "amiami", 200)}
    check(
        "everything unseen is due immediately",
        {"FIGURE-800001-R", "FIGURE-800002-R", "FIGURE-800003-R"} <= due,
        due,
    )
    shelfwatch.promote(db, plain, commit=True)
    check("promotion puts a product at the front", plain.shelf_due_at is not None)

    # A new-condition listing is one item at one price: nothing to follow.
    fresh = Item(provider="amiami", code="FIG-NEW-1", name="New", condition=Condition.new)
    db.add(fresh)
    db.commit()
    codes = {i.code for i in shelfwatch.due_items(db, "amiami", 200)}
    check("first-hand listings are left alone", "FIG-NEW-1" not in codes, codes)

    db.query(Watch).filter(Watch.user_id == user.id).delete()
    db.delete(user)
    db.commit()
    _purge(db, "FIGURE-800001-R", "FIGURE-800002-R", "FIGURE-800003-R", "FIG-NEW-1")
    db.close()


def test_history_pruning_keeps_the_irreplaceable() -> None:
    print("\n== Pruning never eats what cannot be fetched again ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item, Listing, ListingStatus, PricePoint
    from app.services import catalog

    init_db()
    db = SessionLocal()
    old = datetime.now(timezone.utc) - timedelta(days=2000)

    def series(item, prices, listing=None):
        for offset, price in enumerate(prices):
            db.add(
                PricePoint(
                    item=item,
                    listing=listing,
                    recorded_at=old + timedelta(days=offset),
                    price=float(price),
                    currency="JPY",
                    in_stock=True,
                )
            )

    live = Item(provider="amiami", code="PRUNE-LIVE", name="Still on sale",
                condition=Condition.preowned, order_closed=False)
    gone = Item(provider="amiami", code="PRUNE-GONE", name="Deleted upstream",
                condition=Condition.preowned, order_closed=True)
    withcopy = Item(provider="amiami", code="PRUNE-COPY", name="Has a copy trail",
                    condition=Condition.preowned, order_closed=False)
    db.add_all([live, gone, withcopy])
    db.commit()

    copy = Listing(item=withcopy, provider="amiami", code="PRUNE-COPY-R7",
                   sequence=7, price=1000.0, last_price=900.0, currency="JPY",
                   status=ListingStatus.gone)
    db.add(copy)
    db.commit()

    series(live, [500, 400, 700, 300, 650, 550])
    series(gone, [900, 800, 1100])
    series(withcopy, [200, 210])
    series(withcopy, [1000, 950, 900], listing=copy)
    db.commit()

    before = db.query(PricePoint).count()
    removed = catalog.prune_history(db, retention_days=365)
    check("something old was removed", removed > 0, removed)

    kept = db.query(PricePoint).filter(PricePoint.item_id == gone.id).count()
    check("a delisted item keeps its whole history", kept == 3, kept)

    copy_points = db.query(PricePoint).filter(PricePoint.listing_id == copy.id).count()
    check("a copy's own price trail is untouched", copy_points == 3, copy_points)

    live_prices = sorted(
        p.price
        for p in db.query(PricePoint).filter(
            PricePoint.item_id == live.id, PricePoint.listing_id.is_(None)
        )
    )
    check("the cheapest observation survives", 300.0 in live_prices, live_prices)
    check("so does the dearest", 700.0 in live_prices, live_prices)
    check("and the first", 500.0 in live_prices, live_prices)
    check("and the last", 550.0 in live_prices, live_prices)
    check("but redundant middles went", len(live_prices) < 6, live_prices)

    # Every item must still be able to answer "what did this ever cost".
    stats = catalog.price_stats(db, live.id)
    check("the lowest ever is still reportable", stats["lowest"] == 300.0, stats)
    check("and the highest", stats["highest"] == 700.0, stats)

    check(
        "nothing was removed from an item we are the last record of",
        db.query(PricePoint).count() == before - removed,
    )

    for row in (live, gone, withcopy):
        db.delete(row)
    db.commit()
    db.close()


def test_deleting_a_copy_keeps_its_prices() -> None:
    print("\n== Losing a copy row must not lose the item's history ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item, Listing, ListingStatus, PricePoint

    init_db()
    db = SessionLocal()
    item = Item(provider="amiami", code="ORPHAN-1", name="Orphan test",
                condition=Condition.preowned)
    db.add(item)
    db.commit()

    copy = Listing(item=item, provider="amiami", code="ORPHAN-1-R3", sequence=3,
                   price=5000.0, last_price=4500.0, currency="JPY",
                   status=ListingStatus.gone)
    db.add(copy)
    db.commit()
    for price in (5000.0, 4700.0, 4500.0):
        db.add(
            PricePoint(item=item, listing=copy, price=price, currency="JPY", in_stock=True)
        )
    db.commit()
    copy_id = copy.id

    check("three observations recorded through the copy",
          db.query(PricePoint).filter(PricePoint.listing_id == copy_id).count() == 3)

    db.delete(copy)
    db.commit()

    survivors = db.query(PricePoint).filter(PricePoint.item_id == item.id).all()
    check("the prices outlive the copy", len(survivors) == 3, len(survivors))
    check(
        "and simply forget which copy they came from",
        all(p.listing_id is None for p in survivors),
        [p.listing_id for p in survivors],
    )

    db.delete(item)
    db.commit()
    db.close()


def test_image_prune_protects_the_unfetchable() -> None:
    print("\n== Image eviction leaves deleted listings alone ==")
    from app.db import SessionLocal, init_db
    from app.models import CachedImage, Condition, Item
    from app.services import images

    init_db()
    db = SessionLocal()

    # Three photos with different prospects. The used copy still on sale is
    # the interesting one: its listing is up now, so its photo could in
    # principle be fetched again - but it is a photograph of that one copy's
    # actual condition, and AmiAmi deletes it the moment the copy sells.
    # Evicting it while it is still listed loses it just as completely as
    # evicting it afterwards, only with a delay.
    live = Item(provider="amiami", code="IMG-LIVE", name="Used, on sale", order_closed=False,
                condition=Condition.preowned, in_stock=True,
                image_url="https://img.amiami.com/images/product/main/live.jpg")
    gone = Item(provider="amiami", code="IMG-GONE", name="Deleted upstream", order_closed=True,
                condition=Condition.preowned,
                image_url="https://img.amiami.com/images/product/main/gone.jpg")
    # A factory-new item's picture is a product shot that stays up for as long
    # as the product exists. That one is replaceable, so that one goes.
    stock = Item(provider="amiami", code="IMG-NEW", name="New, on sale", order_closed=False,
                 condition=Condition.new, in_stock=True,
                 image_url="https://img.amiami.com/images/product/main/new.jpg")
    db.add_all([live, gone, stock])
    db.commit()

    keys = {}
    for item, age in ((gone, 100), (live, 50), (stock, 1)):
        for url in images.urls_for_item(item):
            key = images.key_for(url)
            keys.setdefault(item.code, set()).add(key)
            db.add(
                CachedImage(
                    key=key,
                    source_url=url,
                    content_type="image/jpeg",
                    bytes=5_000_000,
                    last_used_at=datetime.now(timezone.utc) - timedelta(days=age),
                )
            )
    db.commit()

    # A budget far under what is stored, so eviction has to do something.
    result = images.prune(db, budget_bytes=1_000_000)
    remaining = {row.key for row in db.query(CachedImage).all()}

    check(
        "the deleted listing's photos are still there",
        keys["IMG-GONE"] <= remaining,
        sorted(remaining),
    )
    check(
        "even though they were the least recently used",
        result["protected_kept"] >= len(keys["IMG-GONE"]),
        result,
    )
    check(
        "a used copy still on sale is kept too, before its photo can be lost",
        keys["IMG-LIVE"] <= remaining,
        sorted(remaining),
    )
    check(
        "the replaceable product shot was the one evicted",
        not (keys["IMG-NEW"] & remaining),
        sorted(remaining),
    )
    check(
        "and staying over budget is reported rather than hidden",
        result["over_budget"] > 0,
        result,
    )

    db.query(CachedImage).delete()
    for row in (live, gone, stock):
        db.delete(row)
    db.commit()
    db.close()


def test_price_survives_a_quiet_response() -> None:
    print("\n== A shop response with no price does not erase one ==")
    from app.db import SessionLocal, init_db
    from app.providers import NormalizedItem
    from app.services import catalog

    init_db()
    db = SessionLocal()

    def push(price, price_max=None, closed=False, in_stock=True):
        return catalog.upsert_item(
            db,
            NormalizedItem(
                provider="amiami",
                code="QUIET-1",
                name="Quiet response",
                url="https://www.amiami.com/eng/detail/?gcode=QUIET-1",
                currency="JPY",
                price=price,
                price_max=price_max,
                condition="preowned",
                in_stock=in_stock,
                order_closed=closed,
            ),
        )[0]

    item = push(4200.0, 5200.0)
    check("the price is recorded", item.current_price == 4200.0)

    item = push(None, None, closed=True, in_stock=False)
    check(
        "a sold-out response keeps the last price we saw",
        item.current_price == 4200.0,
        item.current_price,
    )
    check("and its range", item.price_max == 5200.0, item.price_max)
    check("while availability does move", item.order_closed is True)

    db.delete(item)
    db.commit()
    db.close()


def test_wishlist_covers_both_conditions() -> None:
    print("\n== A wishlist entry is about the figure ==")
    from app.db import SessionLocal, init_db
    from app.models import (
        CollectionEntry,
        CollectionStatus,
        Condition,
        Item,
        User,
        UserRole,
    )
    from app.services import catalog

    init_db()
    db = SessionLocal()
    user = User(username="wish", email="w@example.com", password_hash="x", role=UserRole.user)
    db.add(user)
    db.commit()

    # The figure exists twice: new but sold out, and used but on the shelf.
    # Wishlisting the only listing that existed when you first saw it should
    # not mean never hearing that it came back second-hand.
    new = Item(
        provider="amiami", code="WISH-1", name="Miku", condition=Condition.new,
        current_price=12800.0, in_stock=False, order_closed=True,
    )
    used = Item(
        provider="amiami", code="WISH-1-R", name="Miku", condition=Condition.preowned,
        current_price=8400.0, in_stock=True,
    )
    db.add_all([new, used])
    db.commit()
    db.add(
        CollectionEntry(user_id=user.id, item_id=new.id, status=CollectionStatus.wishlist)
    )
    db.commit()

    available = catalog.wishlist_available(db, user.id)
    check("saving the new listing surfaces the used one", len(available) == 1, available)
    check("and it is the one you can actually buy", available[0].code == "WISH-1-R")

    # Both on sale: one row, the cheaper of the pair.
    new.in_stock = True
    new.order_closed = False
    db.commit()
    available = catalog.wishlist_available(db, user.id)
    check("with both on sale the figure appears once", len(available) == 1, available)
    check("as the cheaper listing", available[0].code == "WISH-1-R")

    # Nothing buyable means nothing to show, rather than a stale row.
    new.in_stock = False
    used.in_stock = False
    db.commit()
    check("nothing on sale shows nothing", catalog.wishlist_available(db, user.id) == [])

    check("codes reduce to one figure", catalog.figure_code("WISH-1-R") == "WISH-1")
    check("and a new code is already the figure", catalog.figure_code("WISH-1") == "WISH-1")
    check("the counterpart flips both ways", catalog.counterpart_code("WISH-1") == "WISH-1-R")
    check("and back", catalog.counterpart_code("WISH-1-R") == "WISH-1")

    db.query(CollectionEntry).filter(CollectionEntry.user_id == user.id).delete()
    db.delete(user)
    db.commit()
    _purge(db, "WISH-1", "WISH-1-R")
    db.close()


def test_search_can_fold_conditions() -> None:
    print("\n== Search can show one row per figure ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item
    from app.schemas import LocalSearchRequest
    from app.services import localsearch

    init_db()
    db = SessionLocal()
    rows = [
        ("FOLD-1", 12000.0, True, Condition.new),
        ("FOLD-1-R", 8400.0, True, Condition.preowned),
        ("FOLD-2", 5000.0, False, Condition.new),
        ("FOLD-2-R", 4200.0, True, Condition.preowned),
        ("FOLD-3", 9000.0, True, Condition.new),
    ]
    for code, price, stock, condition in rows:
        db.add(
            Item(
                provider="amiami",
                code=code,
                figure_code=code[:-2] if code.endswith("-R") else code,
                name="Folding test " + code,
                current_price=price,
                in_stock=stock,
                condition=condition,
            )
        )
    db.commit()

    def codes(**kwargs):
        result = localsearch.search(db, LocalSearchRequest(q="Folding test", **kwargs))
        return [item.code for item in result.items], result.total

    listed, total = codes()
    check("without folding every listing appears", total == 5, (listed, total))

    folded, total = codes(combine_conditions=True)
    check("folded, one row per figure", total == 3, (folded, total))
    check("the used listing wins when it is cheaper", "FOLD-1-R" in folded)
    check("and the new one is dropped", "FOLD-1" not in folded, folded)
    check(
        "an out-of-stock listing loses to its in-stock twin",
        "FOLD-2-R" in folded and "FOLD-2" not in folded,
        folded,
    )
    check("a figure with only one listing still shows", "FOLD-3" in folded)

    _purge(db, *[code for code, *_ in rows])
    db.close()


def test_timestamps_keep_their_zone() -> None:
    print("\n== Timestamps say which zone they are in ==")
    from app.db import SessionLocal, init_db
    from app.models import Item

    init_db()
    db = SessionLocal()
    db.add(Item(provider="amiami", code="TZ-1", name="Timezone test"))
    db.commit()
    db.expire_all()

    row = db.query(Item).filter(Item.code == "TZ-1").one()
    check("a stored timestamp comes back aware", row.first_seen_at.tzinfo is not None)

    # This is the whole point: a naive timestamp serialises without an offset,
    # and a browser reads that as its own local time. Every date in the
    # interface was therefore wrong by the viewer's offset, which is how a
    # slice that had just run reported having run two hours ago for ever.
    import json

    from app.api.serializers import item_out

    emitted = json.loads(item_out(db, row).model_dump_json())["first_seen_at"]
    check(
        "and serialises with its offset attached",
        emitted.endswith("Z") or "+" in emitted[10:],
        emitted,
    )

    _purge(db, "TZ-1")
    db.close()


def test_blocklist_hides_things_while_browsing() -> None:
    print("\n== A blocklist hides things while browsing ==")
    from app.db import SessionLocal, init_db
    from app.models import Item
    from app.schemas import LocalSearchRequest
    from app.services import localsearch

    init_db()
    db = SessionLocal()
    names = {
        "BLK-1": "Nendoroid Hatsune Miku",
        "BLK-2": "Hatsune Miku 1/7 Scale Figure",
        "BLK-3": "figma Kagamine Rin",
    }
    for code, name in names.items():
        db.add(Item(provider="amiami", code=code, name=name))
    db.commit()

    def found(**kwargs):
        result = localsearch.search(db, LocalSearchRequest(q="BLK", **kwargs))
        return {item.code for item in result.items}

    def by_name(**kwargs):
        result = localsearch.search(db, LocalSearchRequest(**kwargs))
        return {i.code for i in result.items if i.code in names}

    check("everything shows with nothing blocked", by_name() == set(names))
    check(
        "a blocked word hides its figures",
        by_name(blocked_terms=["nendoroid"]) == {"BLK-2", "BLK-3"},
        by_name(blocked_terms=["nendoroid"]),
    )
    check(
        "blocking is case-insensitive",
        by_name(blocked_terms=["NENDOROID"]) == {"BLK-2", "BLK-3"},
    )
    check(
        "several words can be blocked at once",
        by_name(blocked_terms=["nendoroid", "figma"]) == {"BLK-2"},
    )
    check(
        "and one search can see past the list",
        by_name(blocked_terms=["nendoroid"], ignore_blocklist=True) == set(names),
    )
    check(
        "a word that matches nothing hides nothing",
        by_name(blocked_terms=["gundam"]) == set(names),
    )

    _purge(db, *names)
    db.close()


def test_rail_filters_take_lists() -> None:
    print("\n== A rail hands over everything it was built from ==")
    from app.db import SessionLocal, init_db
    from app.models import Item
    from app.schemas import LocalSearchRequest
    from app.services import localsearch

    init_db()
    db = SessionLocal()
    rows = {
        "RAIL-1": "Vocaloid",
        "RAIL-2": "Fate/Grand Order",
        "RAIL-3": "Azur Lane",
    }
    for code, series in rows.items():
        db.add(Item(provider="amiami", code=code, name="Rail test " + code, series=series))
    db.commit()

    def found(**kwargs):
        result = localsearch.search(db, LocalSearchRequest(q="Rail test", **kwargs))
        return {item.code for item in result.items}

    # A Discover rail draws on every series you follow. Handing over only the
    # first was why "see all" showed a fraction of the rail it came from.
    check("one series still works", found(series=["Vocaloid"]) == {"RAIL-1"})
    check(
        "several series come back together",
        found(series=["Vocaloid", "Azur Lane"]) == {"RAIL-1", "RAIL-3"},
        found(series=["Vocaloid", "Azur Lane"]),
    )
    check("an empty list filters nothing", found(series=[]) == set(rows))
    check("an unknown series matches nothing", found(series=["Nothing At All"]) == set())

    _purge(db, *rows)
    db.close()


def test_null_lists_never_break_a_response() -> None:
    print("\n== One unset column must not empty a page ==")
    from app.schemas import AlertOut, CostProfileOut
    from app.models import TriggerType

    # A JSON column added by an upgrade is NULL on every pre-existing row,
    # because its default is a callable that cannot be written into an ALTER.
    # The database is repaired on the next start, but a response model that
    # refuses None fails the whole endpoint rather than the single field, and
    # that is how a dashboard went blank after gaining an alert column.
    alert = AlertOut.model_validate(
        {
            "id": 1,
            "trigger": TriggerType.new_match,
            "reasons": None,
            "title": "A new listing matches",
            "body": "",
            "price": 8400.0,
            "currency": "JPY",
            "landed_currency": "EUR",
            "landed_price": None,
            "previous_price": None,
            "watch_id": None,
            "item_id": None,
            "url": None,
            "image_url": None,
            "extra": {},
            "read_at": None,
            "created_at": datetime.now(timezone.utc),
        }
    )
    check("an alert with no reasons still serialises", alert.reasons == [])

    profile = CostProfileOut.model_validate(
        {
            "country": "DE",
            "vat_rate": 0.19,
            "duty_rate": 0.047,
            "duty_free_threshold": 150.0,
            "vat_free_threshold": 0.0,
            "customs_handling_fee": 6.0,
            "shipping_mode": "amiami",
            "shipping_zone": "zone3",
            "shipping_service": "auto_air",
            "shipping_flat": 25.0,
            "shipping_table": [],
            "default_weight_grams": 800,
            "packaging_grams": 250,
            "weight_scale": 1.0,
            "blocked_terms": None,
            "blocked_tags": None,
            "category_weights": {},
            "consolidate_shipping": False,
            "fx_markup": 0.015,
        }
    )
    check("a profile with no blocklist still serialises", profile.blocked_terms == [])
    check("both lists, not just the first", profile.blocked_tags == [])


def test_slices_take_turns() -> None:
    print("\n== Due work finishes, the long sweep fills the gaps ==")
    from app.db import SessionLocal, init_db
    from app.models import CatalogCrawl
    from app.services import crawler

    init_db()
    db = SessionLocal()
    crawler.ensure_scopes(db, "amiami")

    rows = {c.scope: c for c in db.query(CatalogCrawl).all()}
    check("the shop statuses are set up", len(rows) == 4, sorted(rows))
    check(
        "every slice starts enabled",
        all(row.enabled for row in rows.values()),
        {s: r.enabled for s, r in rows.items()},
    )

    # Only pre-owned reads the intake ordering. It was measured on that slice
    # and nowhere else, and for a slice that reads every page the ordering
    # makes no difference anyway.
    check(
        "the pre-owned slice reads the ordering that tracks intake",
        (rows["figures_preowned"].query or {}).get("sort") == "updated",
        {s: (r.query or {}).get("sort") for s, r in rows.items()},
    )
    check(
        "and the others are left on what they had",
        all(
            (rows[s].query or {}).get("sort") == "newest"
            for s in ("figures_in_stock", "figures_preorder", "figures_all")
        ),
    )

    # One slice, two settings: how often the front is re-read, and how often
    # the whole thing is. Re-reading a front only pays when the front is where
    # things turn up, which is true of this ordering and of no other tested.
    preowned = rows["figures_preowned"]
    check("pre-owned has a short pass", preowned.head_pages == 30, preowned.head_pages)
    check(
        "every other slice reads all of itself, every pass",
        all(r.head_pages == 0 for s, r in rows.items() if s != "figures_preowned"),
        {s: r.head_pages for s, r in rows.items()},
    )
    check(
        "the short pass runs far more often than the full sweep",
        crawler.full_sweep_interval_minutes(preowned)
        >= preowned.recheck_interval_minutes * 20,
        (preowned.recheck_interval_minutes, crawler.full_sweep_interval_minutes(preowned)),
    )
    check(
        "and the whole catalogue is rarer still",
        rows["figures_all"].recheck_interval_minutes
        > crawler.full_sweep_interval_minutes(preowned),
    )

    pages = {
        "figures_preowned": 213,
        "figures_in_stock": 59,
        "figures_preorder": 45,
        "figures_all": 1385,
    }
    from app.config import settings as _settings

    def cost_per_day(scope, row):
        """Pages a day: short passes at their interval, plus the full sweeps."""
        head = row.head_pages or 0
        if head <= 0:
            return pages[scope] * (1440 / row.recheck_interval_minutes)
        sweeps = 1440 / crawler.full_sweep_interval_minutes(row)
        shorts = (1440 / row.recheck_interval_minutes) - sweeps
        return head * max(0, shorts) + pages[scope] * sweeps

    per_day = sum(cost_per_day(s, r) for s, r in rows.items() if r.enabled)
    capacity = _settings.crawler_requests_per_minute * 60 * 24
    check(
        "and the schedule leaves budget for everything else",
        per_day < capacity * 0.75,
        f"{per_day:.0f} of {capacity:.0f} pages a day",
    )
    # The point of the rearrangement: an hourly full sweep cost 213 pages an
    # hour to catch arrivals. The short pass catches most of them for 60.
    old_hourly_sweep = 213 * 24
    check(
        "watching for arrivals costs a fraction of what it did",
        cost_per_day("figures_preowned", preowned) < old_hourly_sweep / 3,
        f"{cost_per_day('figures_preowned', preowned):.0f} pages a day "
        f"against {old_hourly_sweep}",
    )

    # --- who gets the next few minutes -------------------------------------
    now = datetime.now(timezone.utc)
    for row in rows.values():
        row.cycles_completed = 1
        row.cursor_page = 1
        row.finished_at = now - timedelta(minutes=1)
        row.last_run_at = now - timedelta(minutes=1)
    db.commit()
    check("with nothing due, nothing is picked", crawler._select_crawl(db, "amiami") is None)

    # The big sweep is part way through and not due, so it fills the gap.
    rows["figures_all"].cursor_page = 400
    db.commit()
    picked = crawler._select_crawl(db, "amiami")
    check(
        "a sweep already under way continues when nothing is due",
        picked is not None and picked.scope == "figures_all",
        picked.scope if picked else None,
    )

    # The half-hourly head comes due and takes over. The big one keeps its
    # cursor. This is the case the whole arrangement exists for: catching an
    # arrival must not wait behind a sweep that has hours left to run.
    rows["figures_preowned"].finished_at = now - timedelta(hours=3)
    db.commit()
    picked = crawler._select_crawl(db, "amiami")
    check(
        "due work outranks a sweep in progress",
        picked.scope == "figures_preowned",
        picked.scope if picked else None,
    )

    # And it stays chosen until its own pass completes, rather than being
    # swapped out every few minutes. That churn is what made this
    # incomprehensible to watch: nothing ever finished.
    rows["figures_preowned"].cursor_page = 12
    rows["figures_preowned"].last_run_at = datetime.now(timezone.utc)
    db.commit()
    check(
        "and keeps the budget until that pass is done",
        crawler._select_crawl(db, "amiami").scope == "figures_preowned",
        crawler._select_crawl(db, "amiami").scope,
    )

    # Once it finishes, the interrupted sweep picks up where it left off.
    rows["figures_preowned"].cursor_page = 1
    rows["figures_preowned"].finished_at = datetime.now(timezone.utc)
    db.commit()
    resumed = crawler._select_crawl(db, "amiami")
    check(
        "then the long sweep resumes from its cursor",
        resumed.scope == "figures_all" and resumed.cursor_page == 400,
        (resumed.scope, resumed.cursor_page),
    )

    # A slice that has never run outranks everything, so a fresh install
    # starts building rather than waiting out an interval.
    rows["figures_preorder"].cycles_completed = 0
    rows["figures_preorder"].finished_at = None
    db.commit()
    check(
        "a slice that has never finished a pass goes first",
        crawler._select_crawl(db, "amiami").scope == "figures_preorder",
    )

    detail = crawler.progress(db, "amiami")
    positions = {s["scope"]: s["queue_position"] for s in detail["slices"]}
    check(
        "the view can say who is in line",
        all(p is not None for p in positions.values()),
        positions,
    )

    db.query(CatalogCrawl).delete()
    db.commit()
    db.close()


def test_sweep_estimate_uses_observed_speed() -> None:
    print("\n== The sweep estimate measures rather than assumes ==")
    from app.models import CatalogCrawl
    from app.services.crawler import _eta_seconds, _record_throughput

    # The old estimate multiplied the configured request rate by the share of
    # each interval spent crawling, which assumed one slice had the crawler to
    # itself and that nothing ever paused. It quoted half an hour for work
    # that took an afternoon.
    naive = 208 / 0.8 * 7.5
    check("the old arithmetic said about half an hour", 1700 < naive < 2100, f"{naive:.0f}s")

    without = _eta_seconds(208)
    check(
        "the fallback now allows for the slices sharing",
        without > naive * 2,
        f"{without}s against {naive:.0f}s",
    )

    for rate, expected_hours in ((60, 3.5), (25, 8.3)):
        seconds = _eta_seconds(208, rate)
        check(
            f"at {rate} pages an hour it says about {expected_hours} h",
            abs(seconds / 3600 - expected_hours) < 0.3,
            f"{seconds / 3600:.1f} h",
        )

    check("nothing left means no estimate", _eta_seconds(0, 25) is None)

    # Throughput is measured against wall-clock time including the waiting, so
    # a slice that only runs now and then is not credited with the speed it
    # manages during the few minutes it holds the budget.
    crawl = CatalogCrawl(provider="amiami", scope="s")
    _record_throughput(crawl, 30, datetime.now(timezone.utc) - timedelta(hours=1))
    check("one hour and 30 pages reads as 30 an hour", abs(crawl.pages_per_hour - 30) < 0.5)

    _record_throughput(crawl, 10, datetime.now(timezone.utc) - timedelta(hours=1))
    check(
        "a slower run pulls the average down without jumping to it",
        10 < crawl.pages_per_hour < 30,
        crawl.pages_per_hour,
    )

    # A gap long enough to be a restart says nothing about throughput.
    steady = crawl.pages_per_hour
    _record_throughput(crawl, 50, datetime.now(timezone.utc) - timedelta(days=2))
    check("a restart-sized gap is ignored", crawl.pages_per_hour == steady)
    _record_throughput(crawl, 0, datetime.now(timezone.utc) - timedelta(hours=1))
    check("and so is a run that fetched nothing", crawl.pages_per_hour == steady)


def test_slice_counts_compare_like_with_like() -> None:
    print("\n== Slice counts mean what the shop's numbers mean ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item
    from app.services import crawler

    init_db()
    db = SessionLocal()

    # Every pre-owned listing carries the shop's in-stock flag - all hundred
    # sampled did, because a used copy that sells is deleted rather than
    # marked gone. The in-stock slice asks the shop for something else
    # entirely, "first-hand stock available", which no used listing has. So
    # counting our stored in_stock swept the whole used catalogue into the
    # in-stock total and reported 13,834 held against 2,813 listed, as if
    # eleven thousand rows had gone stale.
    for index in range(12):
        db.add(
            Item(provider="amiami", code=f"CNT-P{index}", name="used",
                 condition=Condition.preowned, in_stock=True)
        )
    for index in range(5):
        db.add(
            Item(provider="amiami", code=f"CNT-N{index}", name="new",
                 condition=Condition.new, in_stock=True)
        )
    for index in range(3):
        db.add(
            Item(provider="amiami", code=f"CNT-O{index}", name="pre-order",
                 condition=Condition.new, is_preorder=True)
        )
    # A pre-owned listing is in stock and could also read as a pre-order
    # through the other flag; neither may leak into the new-condition counts.
    db.add(
        Item(provider="amiami", code="CNT-PX", name="used pre-order",
             condition=Condition.preowned, in_stock=True, is_preorder=True)
    )
    db.commit()

    def count(scope: str) -> int:
        return crawler.local_count(db, scope, "amiami")

    check("pre-owned counts every used listing", count("figures_preowned") == 13, count("figures_preowned"))
    check(
        "in stock counts first-hand stock only",
        count("figures_in_stock") == 5,
        count("figures_in_stock"),
    )
    check(
        "pre-order does not count used listings either",
        count("figures_preorder") == 3,
        count("figures_preorder"),
    )
    check("and the catch-all counts everything", count("figures_all") == 21, count("figures_all"))

    _purge(
        db,
        *[f"CNT-P{i}" for i in range(12)],
        *[f"CNT-N{i}" for i in range(5)],
        *[f"CNT-O{i}" for i in range(3)],
        "CNT-PX",
    )
    db.close()


def test_activity_can_start_again() -> None:
    print("\n== The activity profile can be started again ==")
    from app.db import SessionLocal, init_db
    from app.models import Item
    from app.services import crawler

    init_db()
    db = SessionLocal()

    # The first catalogue build ruins the profile: every item is first seen
    # while the crawler works through the pages, so the busiest hour reads as
    # whenever the sweep ran rather than when the shop listed anything.
    stale = datetime.now(timezone.utc) - timedelta(days=3)
    for index in range(20):
        db.add(
            Item(provider="amiami", code=f"ACT-{index}", name="crawled", first_seen_at=stale)
        )
    db.commit()

    check("the build shows up in the profile", crawler.activity_profile(db)["new_listings"] == 20)
    check("with no baseline set", crawler.activity_profile(db)["baseline"] is None)

    at = crawler.reset_activity(db)
    profile = crawler.activity_profile(db)
    check("resetting hides what came before", profile["new_listings"] == 0, profile["new_listings"])
    check("and records when the line was drawn", profile["baseline"] is not None)
    check("which is what the reset returned", abs((profile["baseline"] - at).total_seconds()) < 2)

    # Nothing may be deleted: these timestamps belong to the catalogue and the
    # price history, which have their own reasons to exist.
    check("nothing was deleted", db.query(Item).filter(Item.code.like("ACT-%")).count() == 20)

    db.add(Item(provider="amiami", code="ACT-NEW", name="genuinely new"))
    db.commit()
    check(
        "and anything after the line counts again",
        crawler.activity_profile(db)["new_listings"] == 1,
    )

    # A window shorter than the baseline still wins, so "last 7 days" cannot
    # drag data back in from before the reset.
    check(
        "a longer window does not reach past the line",
        crawler.activity_profile(db, days=90)["new_listings"] == 1,
    )

    _purge(db, *[f"ACT-{i}" for i in range(20)], "ACT-NEW")
    db.close()


def test_discover_ignores_placeholder_series() -> None:
    print("\n== Discover ignores the shop's no-franchise placeholder ==")
    from app.services.feed import _is_placeholder

    # The shop files a figure with no franchise under "Original", so treating
    # that as a series someone follows filled the rail with unrelated figures:
    # saving one original character made every other maker's look like a
    # match.
    for value in (
        "Original",
        "original",
        "ORIGINAL",
        "Original Character",
        "Original Character: Vertex",
        "Original: Something",
    ):
        check(f"{value!r} is treated as no series", _is_placeholder(value), value)

    # Deliberately strict, so a real franchise beginning with the word stays.
    for value in ("Original Sin", "Originals of Nature", "Azur Lane", "hololive", ""):
        check(f"{value!r} is left alone", not _is_placeholder(value), value)


def test_slice_counts_compare_like_with_like() -> None:
    print("\n== Slice counts mean what the shop's numbers mean ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item
    from app.services import crawler

    init_db()
    db = SessionLocal()

    # Every pre-owned listing carries the shop's in-stock flag - all hundred
    # sampled did, because a used copy that sells is deleted rather than
    # marked gone. The in-stock slice asks the shop for something else
    # entirely, "first-hand stock available", which no used listing has. So
    # counting our stored in_stock swept the whole used catalogue into the
    # in-stock total and reported 13,834 held against 2,813 listed, as if
    # eleven thousand rows had gone stale.
    for index in range(12):
        db.add(
            Item(provider="amiami", code=f"CNT-P{index}", name="used",
                 condition=Condition.preowned, in_stock=True)
        )
    for index in range(5):
        db.add(
            Item(provider="amiami", code=f"CNT-N{index}", name="new",
                 condition=Condition.new, in_stock=True)
        )
    for index in range(3):
        db.add(
            Item(provider="amiami", code=f"CNT-O{index}", name="pre-order",
                 condition=Condition.new, is_preorder=True)
        )
    # A pre-owned listing is in stock and could also read as a pre-order
    # through the other flag; neither may leak into the new-condition counts.
    db.add(
        Item(provider="amiami", code="CNT-PX", name="used pre-order",
             condition=Condition.preowned, in_stock=True, is_preorder=True)
    )
    db.commit()

    def count(scope: str) -> int:
        return crawler.local_count(db, scope, "amiami")

    check("pre-owned counts every used listing", count("figures_preowned") == 13, count("figures_preowned"))
    check(
        "in stock counts first-hand stock only",
        count("figures_in_stock") == 5,
        count("figures_in_stock"),
    )
    check(
        "pre-order does not count used listings either",
        count("figures_preorder") == 3,
        count("figures_preorder"),
    )
    check("and the catch-all counts everything", count("figures_all") == 21, count("figures_all"))

    _purge(
        db,
        *[f"CNT-P{i}" for i in range(12)],
        *[f"CNT-N{i}" for i in range(5)],
        *[f"CNT-O{i}" for i in range(3)],
        "CNT-PX",
    )
    db.close()


def test_activity_can_start_again() -> None:
    print("\n== The activity profile can be started again ==")
    from app.db import SessionLocal, init_db
    from app.models import Item
    from app.services import crawler

    init_db()
    db = SessionLocal()

    # The first catalogue build ruins the profile: every item is first seen
    # while the crawler works through the pages, so the busiest hour reads as
    # whenever the sweep ran rather than when the shop listed anything.
    stale = datetime.now(timezone.utc) - timedelta(days=3)
    for index in range(20):
        db.add(
            Item(provider="amiami", code=f"ACT-{index}", name="crawled", first_seen_at=stale)
        )
    db.commit()

    check("the build shows up in the profile", crawler.activity_profile(db)["new_listings"] == 20)
    check("with no baseline set", crawler.activity_profile(db)["baseline"] is None)

    at = crawler.reset_activity(db)
    profile = crawler.activity_profile(db)
    check("resetting hides what came before", profile["new_listings"] == 0, profile["new_listings"])
    check("and records when the line was drawn", profile["baseline"] is not None)
    check("which is what the reset returned", abs((profile["baseline"] - at).total_seconds()) < 2)

    # Nothing may be deleted: these timestamps belong to the catalogue and the
    # price history, which have their own reasons to exist.
    check("nothing was deleted", db.query(Item).filter(Item.code.like("ACT-%")).count() == 20)

    db.add(Item(provider="amiami", code="ACT-NEW", name="genuinely new"))
    db.commit()
    check(
        "and anything after the line counts again",
        crawler.activity_profile(db)["new_listings"] == 1,
    )

    # A window shorter than the baseline still wins, so "last 7 days" cannot
    # drag data back in from before the reset.
    check(
        "a longer window does not reach past the line",
        crawler.activity_profile(db, days=90)["new_listings"] == 1,
    )

    _purge(db, *[f"ACT-{i}" for i in range(20)], "ACT-NEW")
    db.close()


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

    # Sorting moved to the shop, which does it across the whole result set
    # rather than the fifty rows we happen to be holding. So the local sort's
    # job now is to keep its hands off anything already ordered upstream.
    from app.providers.amiami import SORT_KEYS, SORT_KEYS_NEEDING_LOCAL_SORT

    check("cheapest is the shop's job now", "price_asc" in SORT_KEYS)
    untouched = AmiAmiProvider._apply_local_sort(items, "price_asc")
    check(
        "so the page comes back in the order it arrived",
        [i.price for i in untouched] == [i.price for i in items],
        [i.price for i in untouched],
    )

    # "priced" selects the dear listings from the whole result but hands them
    # back shuffled, so that one page does still get ordered here.
    check("dearest needs a local pass", "price_desc" in SORT_KEYS_NEEDING_LOCAL_SORT)
    descending = AmiAmiProvider._apply_local_sort(items, "price_desc")
    check("price descending", [i.price for i in descending] == [300, 200, 100, 50])

    # No upstream equivalent at all, so this is ours entirely.
    check("discount has no shop equivalent", "discount" not in SORT_KEYS)
    discounted = AmiAmiProvider._apply_local_sort(items, "discount")
    check("and is still sorted here", len(discounted) == len(items))


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

    # A short pass re-reads the front, but only where the front is worth
    # re-reading. That was measured per ordering, and holds for exactly one of
    # them: under "preowned" a 30-page front held three quarters of the known
    # arrivals, under "regtimed" nine of 594.
    later = CatalogCrawl(
        provider="amiami",
        scope="s",
        pages_total=200,
        cycles_completed=1,
        head_pages=20,
        full_sweep_interval_minutes=1440,
        last_full_sweep_at=datetime.now(timezone.utc),
    )
    check(
        "between sweeps only the front is re-read",
        _page_limit(later) == 20,
        _page_limit(later),
    )

    # Zero is the honest setting for a slice whose ordering puts nothing
    # useful at the front, and it is what the other three carry.
    no_head = CatalogCrawl(
        provider="amiami",
        scope="s",
        pages_total=200,
        cycles_completed=1,
        head_pages=0,
        last_full_sweep_at=datetime.now(timezone.utc),
    )
    check(
        "a slice with no front reads all of itself every pass",
        _page_limit(no_head) == 200,
        _page_limit(no_head),
    )

    # A sweep already under way must not be truncated when the short-pass
    # interval elapses half way through it. Which kind of pass is running is
    # recorded when it starts rather than inferred from the cursor, because a
    # short pass at page 31 of a 30-page front and a sweep at page 31 look
    # identical from the outside.
    mid_sweep = CatalogCrawl(
        provider="amiami",
        scope="s",
        pages_total=200,
        cycles_completed=1,
        head_pages=20,
        cursor_page=90,
        sweeping_all=True,
        full_sweep_interval_minutes=1440,
        last_full_sweep_at=datetime.now(timezone.utc),
    )
    check(
        "a sweep already past the front finishes as a sweep",
        _page_limit(mid_sweep) == 200,
        _page_limit(mid_sweep),
    )

    short = CatalogCrawl(
        provider="amiami", scope="s", pages_total=8, cycles_completed=1, head_pages=20,
        full_sweep_interval_minutes=1440, last_full_sweep_at=datetime.now(timezone.utc),
    )
    check("a slice shorter than its front is not padded out", _page_limit(short) == 8)

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

    # The interval moved to minutes so both settings share one control; a row
    # written before that keeps the schedule it was given.
    from app.services.crawler import full_sweep_interval_minutes

    check(
        "an older row still reads in days",
        full_sweep_interval_minutes(stale) == 7 * 1440,
        full_sweep_interval_minutes(stale),
    )
    stale.full_sweep_interval_minutes = 720
    check(
        "and minutes win once set",
        full_sweep_interval_minutes(stale) == 720,
    )

    check("nothing left means no estimate", _eta_seconds(0) is None)
    check("an estimate is produced when work remains", (_eta_seconds(100) or 0) > 0)
    check(
        "more pages take longer",
        (_eta_seconds(200) or 0) > (_eta_seconds(100) or 0),
    )


def test_crawler_cooldown() -> None:
    print()
    print("== Resting between passes ==")
    from datetime import timedelta as td

    from app.models import CatalogCrawl
    from app.services.crawler import _cooldown_remaining

    now = datetime.now(timezone.utc)

    never_run = CatalogCrawl(provider="amiami", scope="s", cursor_page=1, cycles_completed=0)
    check("a slice that never ran starts immediately", _cooldown_remaining(never_run) == 0)

    mid_pass = CatalogCrawl(
        provider="amiami", scope="s", cursor_page=7, cycles_completed=3,
        finished_at=now, recheck_interval_minutes=30,
    )
    check("a pass under way is never held back", _cooldown_remaining(mid_pass) == 0)

    just_finished = CatalogCrawl(
        provider="amiami", scope="s", cursor_page=1, cycles_completed=3,
        finished_at=now, recheck_interval_minutes=30,
    )
    remaining = _cooldown_remaining(just_finished)
    check("a finished pass rests", 1700 <= remaining <= 1800, remaining)

    rested = CatalogCrawl(
        provider="amiami", scope="s", cursor_page=1, cycles_completed=3,
        finished_at=now - td(minutes=31), recheck_interval_minutes=30,
    )
    check("and starts again once the interval has passed", _cooldown_remaining(rested) == 0)

    # The interval is what stops a slice looping over its first pages: without
    # it, one slice managed 43 passes in an afternoon.
    slow = CatalogCrawl(
        provider="amiami", scope="s", cursor_page=1, cycles_completed=1,
        finished_at=now, recheck_interval_minutes=240,
    )
    check("a longer interval rests longer", _cooldown_remaining(slow) > 14000, _cooldown_remaining(slow))

    missing = CatalogCrawl(provider="amiami", scope="s", cursor_page=1, cycles_completed=1)
    check("no recorded finish means no wait", _cooldown_remaining(missing) == 0)


def test_crawler_coverage_counting() -> None:
    print()
    print("== Coverage counts rows, not page reads ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item
    from app.services.crawler import local_count

    init_db()
    db = SessionLocal()
    try:
        for n in range(7):
            db.add(
                Item(
                    provider="amiami",
                    code=f"COVER-{n}",
                    name=f"Item {n}",
                    condition=Condition.preowned if n < 4 else Condition.new,
                    in_stock=n % 2 == 0,
                    is_preorder=n == 6,
                )
            )
        db.commit()

        check("pre-owned slice counts pre-owned rows", local_count(db, "figures_preowned", "amiami") == 4)
        # Four of the seven are in stock, but two of those are pre-owned and
        # the shop's in-stock slice asks for first-hand stock, which no used
        # listing has. Counting them here is what made the panel report five
        # times more held than listed.
        check(
            "in-stock slice counts first-hand stock only",
            local_count(db, "figures_in_stock", "amiami") == 2,
            local_count(db, "figures_in_stock", "amiami"),
        )
        check("pre-order slice counts pre-orders", local_count(db, "figures_preorder", "amiami") == 1)
        check("the catch-all counts everything", local_count(db, "figures_all", "amiami") == 7)
        check("another provider counts nothing here", local_count(db, "figures_all", "mandarake") == 0)

        # The point of the change: re-reading pages cannot inflate this.
        before = local_count(db, "figures_preowned", "amiami")
        after = local_count(db, "figures_preowned", "amiami")
        check("counting twice gives the same answer", before == after == 4)
    finally:
        db.close()


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


def test_local_search() -> None:
    print()
    print("== Searching the local catalogue ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item, ItemTag, Tag, TagKind
    from app.schemas import LocalSearchRequest
    from app.services import localsearch

    PROVIDER = "localsearch-fixture"

    init_db()
    db = SessionLocal()
    try:
        scale = Tag(kind=TagKind.tag, slug="scale_figure", name="scale figure")
        nendo = Tag(kind=TagKind.tag, slug="nendoroid", name="nendoroid")
        db.add_all([scale, nendo])
        db.flush()

        rows = [
            # name, condition, price, lowest, delisted, tags
            ("Miku Scale Figure", Condition.new, 12000, 12000, False, [scale]),
            ("Miku Nendoroid", Condition.new, 5000, 4000, False, [nendo]),
            ("Rem Scale Figure", Condition.preowned, 8000, 8000, False, [scale]),
            ("Sold Out Rem", Condition.preowned, 6000, 6000, True, [scale, nendo]),
        ]
        # A provider of its own, so counts are not disturbed by rows other
        # tests left in the shared database.
        for n, (name, cond, price, low, closed, tags) in enumerate(rows):
            item = Item(
                provider=PROVIDER,
                code=f"LS-{n}",
                name=name,
                condition=cond,
                current_price=price,
                lowest_price=low,
                list_price=20000,
                in_stock=not closed,
                order_closed=closed,
            )
            db.add(item)
            db.flush()
            for tag in tags:
                db.add(ItemTag(item_id=item.id, tag_id=tag.id))
        db.commit()

        def run(**kw):
            return localsearch.search(db, LocalSearchRequest(provider=PROVIDER, **kw))

        check("everything is returned by default", run().total == 4, run().total)

        # Delisted items stay in the results: that history is the whole point.
        check("a sold-out listing is still findable", run(q="Sold Out").total == 1)
        check("and can be isolated", run(availability="delisted").total == 1)
        check("buyable excludes it", run(availability="buyable").total == 3)

        check("keywords match the name", run(q="Miku").total == 2)
        check("several words must all match", run(q="Miku Scale").total == 1, run(q="Miku Scale").total)
        check("a word matching nothing finds nothing", run(q="Asuka").total == 0)

        check("condition filters", run(condition="preowned").total == 2)
        check("price ranges filter", run(max_price=6000).total == 2, run(max_price=6000).total)

        # Tags: requiring, combining and excluding.
        check("requiring a tag", run(tags=["scale_figure"]).total == 3)
        check("excluding a tag", run(exclude_tags=["nendoroid"]).total == 2)
        check(
            "requiring one and excluding another",
            run(tags=["scale_figure"], exclude_tags=["nendoroid"]).total == 2,
        )
        check(
            "all of several tags",
            run(tags=["scale_figure", "nendoroid"], tag_mode="all").total == 1,
        )
        check(
            "any of several tags",
            run(tags=["scale_figure", "nendoroid"], tag_mode="any").total == 4,
        )
        check("an unknown tag matches nothing", run(tags=["does_not_exist"]).total == 0)

        # Only possible because prices are kept after a listing is removed.
        at_lowest = run(at_lowest_ever=True)
        check("items at their lowest ever", at_lowest.total == 3, at_lowest.total)

        cheapest = run(sort="price_asc")
        check("cheapest first", cheapest.items[0].current_price == 5000, cheapest.items[0].name)
        dearest = run(sort="price_desc")
        check("dearest first", dearest.items[0].current_price == 12000)

        paged = run(per_page=2, page=2)
        check("pagination reports pages", paged.pages == 2, paged.pages)
        check("and returns the right slice", len(paged.items) == 2)
    finally:
        db.close()


def test_watch_code_normalisation() -> None:
    print()
    print("== Product codes name listings, not products ==")
    from app.providers.amiami import AmiAmiProvider

    norm = AmiAmiProvider.normalise_watch_code

    # FIGURE-x is the first-hand listing, FIGURE-x-R the pre-owned one, and
    # FIGURE-x-R032 one specific graded copy of the latter.
    check("a first-hand code is left alone", norm("FIGURE-180385") == "FIGURE-180385")
    check("a pre-owned code is left alone", norm("FIGURE-180385-R") == "FIGURE-180385-R")
    check(
        "a single graded copy reduces to its listing",
        norm("FIGURE-180385-R032") == "FIGURE-180385-R",
    )
    check("case is normalised", norm("figure-180385-r151") == "FIGURE-180385-R")
    check("hyphenated makers survive", norm("FIG-MOE-2210-R170") == "FIG-MOE-2210-R")
    check("empty input stays empty", norm("") == "")
    check("whitespace is trimmed", norm("  FIGURE-1-R007  ") == "FIGURE-1-R")

    # Watching one copy would break the instant that copy sold, which is
    # precisely the moment the watch was there for.
    check(
        "a copy suffix never survives",
        all(not c.rsplit("-R", 1)[-1].isdigit() or c.endswith("-R")
            for c in [norm("A-1-R001"), norm("B-2-R99"), norm("C-3-R")]),
    )


def test_image_fallback() -> None:
    print()
    print("== A photo that has not been cached yet ==")
    from app.db import SessionLocal, init_db
    from app.models import CachedImage, Item
    from app.services import images

    init_db()
    db = SessionLocal()
    try:
        url = "https://img.amiami.com/images/product/main/999/FALLBACK-1.jpg"
        item = Item(provider="fallback-fixture", code="FALLBACK-1", name="X", image_url=url)
        db.add(item)
        db.commit()

        key = images.key_for(url)
        check("nothing is registered to begin with",
              db.query(CachedImage).filter(CachedImage.key == key).first() is None)

        # Registering is what makes the hashed route resolvable at all: the
        # key cannot be reversed, so without this the endpoint has no idea
        # what to fetch and the tile renders blank.
        added = images.register(db, images.urls_for_item(item), commit=True)
        check("registering records the mapping", added >= 1, added)

        row = db.query(CachedImage).filter(CachedImage.key == key).one()
        check("the source URL is recoverable", row.source_url == url)
        check("but nothing has been downloaded", row.fetched_at is None)
        check("and it is not marked gone", row.gone is False)

        # Registering the same photo twice must not duplicate it.
        again = images.register(db, [url], commit=True)
        check("registering twice adds nothing", again == 0)
        check("still one row", db.query(CachedImage).filter(CachedImage.key == key).count() == 1)
    finally:
        db.close()


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


def test_run_log() -> None:
    print("\n== The log records whole passes, not the slots they were spread over ==")
    from app.models import CatalogCrawl
    from app.services.crawler import (
        RUN_LOG_LENGTH,
        CrawlRun,
        _accumulate,
        _record_pass,
    )

    def fresh(started_minutes_ago: float) -> CatalogCrawl:
        crawl = CatalogCrawl(provider="amiami", scope="figures_preowned", recent_runs=[])
        began = datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)
        crawl.current_pass = {
            "started_at": began.isoformat(), "pages": 0, "items": 0, "new": 0,
            "changed": 0, "errors": 0, "slots": 0, "working_seconds": 0.0,
            "interruptions": 0,
        }
        return crawl

    # A real hour from the instance: eleven scheduler slots, 176 pages, the
    # crawler standing aside for a watch on nearly every one. Logged per slot
    # this produced eleven rows, several of them four seconds long, and a
    # pages-per-minute column showing 27 against a configured limit of 8 -
    # because two pages fetched from banked tokens is not a speed.
    slots = ((22, 120, 1), (25, 180, 2), (11, 59, 1), (12, 60, 1), (18, 120, 1),
             (2, 17, 1), (28, 180, 2), (22, 60, 1), (10, 50, 1), (13, 120, 1), (13, 55, 0))
    crawl = fresh(63)
    for pages, seconds, interruptions in slots:
        _accumulate(crawl, CrawlRun(pages=pages, items=pages * 50, new_items=0,
                                    changed=1 if pages > 20 else 0, seconds=seconds,
                                    interruptions=interruptions))
    check("nothing is logged mid-pass", crawl.recent_runs == [], crawl.recent_runs)

    _record_pass(crawl, "read to the end")
    check("finishing writes exactly one row", len(crawl.recent_runs) == 1)

    entry = crawl.recent_runs[0]
    check("holding every page of the pass", entry["pages"] == 176, entry["pages"])
    check("and every slot it took", entry["slots"] == len(slots), entry["slots"])
    check("counting the times it stood aside", entry["interruptions"] == 12,
          entry["interruptions"])

    # Two durations, answering different questions: how long it took in the
    # world, and how much of that was spent fetching rather than waiting.
    check("the elapsed time is the wall clock", 3700 < entry["seconds"] < 3900,
          entry["seconds"])
    check("the working time is far less", entry["working_seconds"] < entry["seconds"] / 2,
          (entry["working_seconds"], entry["seconds"]))
    check(
        "and the two account for the whole of it",
        abs(entry["working_seconds"] + entry["waiting_seconds"] - entry["seconds"]) < 0.2,
        entry,
    )

    # Over the whole pass the rate is one the slice can actually hold, which
    # the per-slot figure never was.
    check("the rate is inside the request limit", entry["pages_per_minute"] < 8,
          entry["pages_per_minute"])
    check("and is not zero", entry["pages_per_minute"] > 0)

    # A pass too short to time honestly reports no rate rather than a number
    # invented by dividing by nearly nothing.
    brief = fresh(0)
    _accumulate(brief, CrawlRun(pages=2, items=100, seconds=4.0))
    _record_pass(brief, "read to the end")
    check("a pass of seconds quotes no rate",
          brief.recent_runs[0]["pages_per_minute"] is None,
          brief.recent_runs[0]["pages_per_minute"])

    # A pass that fetched nothing is not worth a line.
    empty = fresh(5)
    _record_pass(empty, "read to the end")
    check("an empty pass is not logged", empty.recent_runs == [])

    # The log prunes itself rather than needing a job to do it.
    keeper = fresh(10)
    for _ in range(RUN_LOG_LENGTH * 2):
        keeper.current_pass = {
            "started_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            "pages": 5, "items": 250, "new": 0, "changed": 0, "errors": 0,
            "slots": 1, "working_seconds": 60.0, "interruptions": 0,
        }
        _record_pass(keeper, "read to the end")
    check("the log stays capped", len(keeper.recent_runs) == RUN_LOG_LENGTH,
          len(keeper.recent_runs))
    check("and the accumulator is cleared for the next pass",
          keeper.current_pass == {}, keeper.current_pass)

    # A slot arriving with no pass behind it - a cursor resumed after a
    # restart - starts the accounting rather than dropping the work.
    orphan = CatalogCrawl(provider="amiami", scope="s", recent_runs=[], current_pass={})
    _accumulate(orphan, CrawlRun(pages=7, items=350, seconds=40.0))
    check("an orphaned slot still counts", orphan.current_pass["pages"] == 7,
          orphan.current_pass)


def test_request_accounting() -> None:
    print("\n== The request budget is attributed to the job that spent it ==")
    from app.services import reqlog

    reqlog._events.clear()
    for _ in range(12):
        with reqlog.purpose("catalogue"):
            reqlog.record("amiami")
    for _ in range(6):
        with reqlog.purpose("shelf"):
            reqlog.record("amiami")
    with reqlog.purpose("catalogue"):
        reqlog.record("amiami", ok=False)
    for _ in range(8):
        with reqlog.purpose("mfc"):
            reqlog.record("mfc")

    out = reqlog.rates(60)
    ami = out["hosts"]["amiami"]
    check("both hosts are counted apart", set(out["hosts"]) == {"amiami", "mfc"})
    check("every request is attributed", ami["total"] == 19, ami["total"])
    check("failures are visible", ami["errors"] == 1)
    check("shares add up", abs(sum(p["share"] for p in ami["purposes"]) - 100) < 0.5)
    check("the biggest spender comes first", ami["purposes"][0]["key"] == "catalogue")
    check("labels are readable", ami["purposes"][0]["label"] == "Catalogue sweep")

    # A minute of sampling scaled to an hour is a projection, and says so.
    check("an hourly figure from a minute is flagged", out["hourly_is_projected"])
    check("and is the honest multiple", ami["per_hour"] == 19 * 60, ami["per_hour"])
    check("a full hour is not flagged", not reqlog.rates(3600)["hourly_is_projected"])

    # Anything that does not declare itself is still counted, under "other",
    # rather than vanishing and making the totals disagree with reality.
    reqlog.record("amiami")
    check("untagged requests are not lost",
          reqlog.rates(60)["hosts"]["amiami"]["purposes"][-1]["key"] == "other")

    # The context manager restores what was running before it.
    with reqlog.purpose("catalogue"):
        with reqlog.purpose("images"):
            inner = reqlog.current()
        outer = reqlog.current()
    check("nesting unwinds properly", (inner, outer) == ("images", "catalogue"))
    check("and leaves nothing set afterwards", reqlog.current() == "other")

    reqlog._events.clear()


def test_photo_counts_distinguish_known_from_held() -> None:
    print("\n== Photos known of and photos on disk are different numbers ==")
    from app.db import SessionLocal, init_db
    from app.models import CachedImage
    from app.services import images

    init_db()
    db = SessionLocal()
    db.query(CachedImage).delete()
    db.commit()

    # register() writes a row without fetching anything: the public route is a
    # hash of the source URL, so the mapping has to exist before the download
    # can happen. Reporting those rows as "cached" is what produced 131,716
    # photos in 200 MB, which would be 1.5 kB each.
    images.register(db, [f"https://img.amiami.com/{n}.jpg" for n in range(10)], commit=True)
    stats = images.stats(db)
    check("all ten are known of", stats["count"] == 10, stats["count"])
    check("none are downloaded yet", stats["downloaded"] == 0)
    check("and all ten are queued", stats["pending"] == 10)
    check("so nothing counts as covered", stats["coverage_percent"] == 0.0)

    for row in db.query(CachedImage).limit(3).all():
        row.fetched_at = datetime.now(timezone.utc)
        row.bytes = 40_000
    db.commit()
    stats = images.stats(db)
    check("the three fetched ones show as downloaded", stats["downloaded"] == 3)
    check("the rest stay queued", stats["pending"] == 7)
    check(
        "and the average is over the files, not the rows",
        abs(stats["average_bytes"] - 40_000) < 1,
        stats["average_bytes"],
    )

    db.query(CachedImage).delete()
    db.commit()
    db.close()


def test_prefetch_works_through_its_backlog() -> None:
    print("\n== The prefetcher downloads the photos it has only noted ==")
    from app.db import SessionLocal, init_db
    from app.models import CachedImage, CollectionEntry, Condition, Item
    from app.services import images

    init_db()
    db = SessionLocal()
    db.query(CachedImage).delete()
    db.query(CollectionEntry).delete()
    db.query(Item).delete()
    db.commit()

    def add_item(code, *, preowned=False, in_stock=True, days_old=1):
        item = Item(
            provider="amiami",
            code=code,
            name=f"Figure {code}",
            image_url=f"https://img.amiami.com/{code}.jpg",
            in_stock=in_stock,
            condition=Condition.preowned if preowned else Condition.new,
            first_seen_at=datetime.now(timezone.utc) - timedelta(days=days_old),
            last_seen_at=datetime.now(timezone.utc),
        )
        db.add(item)
        db.flush()
        images.register(db, images.urls_for_item(item))
        return item

    wishlisted = [add_item(f"W{n}") for n in range(30)]
    for item in wishlisted:
        db.add(CollectionEntry(user_id=1, item_id=item.id))
    fresh = add_item("P-NEW", preowned=True, days_old=0)
    older = add_item("P-OLD", preowned=True, days_old=40)
    db.commit()

    known = images.stats(db)["count"]
    check("a crawl notes every photo without fetching it", known == 32, known)
    check("none of them are on disk yet", images.stats(db)["downloaded"] == 0)
    check("and all of them are owed", images.pending_count(db) == known)

    # Stand in for the network: record what was asked for, in order.
    asked: list[str] = []

    def pretend_download(url):
        asked.append(url)
        return b"x" * 1000, "image/jpeg"

    original = images._download
    images._download = pretend_download
    try:
        # The wishlist photos are already on disk. Before the fix, they filled
        # the candidate list, every one of them was skipped as "cached", and
        # the run ended having fetched nothing - for ever.
        for item in wishlisted:
            for url in images.urls_for_item(item):
                images.fetch(db, url, touch=False)
        asked.clear()

        result = images.prefetch(db, limit=50)
        check("a second run does not stall behind them", result["fetched"] > 0, result)
        check("it reports what is still owed", result["queued"] < known, result["queued"])
        check(
            "the newest used copy is fetched before the one sitting a month",
            asked and "P-NEW" in asked[0],
            asked[:2],
        )
        check(
            "and both used copies are covered",
            any("P-OLD" in url for url in asked),
        )

        # Nothing is fetched twice.
        before = len(asked)
        images.prefetch(db, limit=50)
        check("what is already on disk is not fetched again", len(asked) == before)
        check("and then nothing is owed", images.pending_count(db) == 0)
    finally:
        images._download = original

    # A photo the shop has deleted is not retried for ever.
    lost = add_item("GONE")
    db.commit()

    def refuse(url):
        raise FileNotFoundError("410 Gone")

    images._download = refuse
    try:
        images.prefetch(db, limit=10)
        check("a deleted photo is marked gone", images.pending_count(db) == 0)
        check(
            "and does not count as downloaded",
            images.stats(db)["gone_upstream"] > 0,
        )
    finally:
        images._download = original

    db.query(CachedImage).delete()
    db.query(CollectionEntry).delete()
    db.query(Item).delete()
    db.commit()
    db.close()


def test_tag_search_reaches_past_the_popular_ones() -> None:
    print("\n== A tag filter search covers every tag, not the top of the list ==")
    from app.db import SessionLocal, init_db
    from app.models import Tag, TagKind
    from app.services.localsearch import facet_tags

    init_db()
    db = SessionLocal()
    db.query(Tag).delete()
    db.commit()

    # What MyFigureCollection linking builds up: a long tail of tags, each
    # used once or twice, under a small head of very common ones.
    for n in range(400):
        db.add(
            Tag(
                kind=TagKind.tag,
                slug=f"common-{n}",
                name=f"Common {n}",
                usage_count=1000 - n,
            )
        )
    db.add(Tag(kind=TagKind.tag, slug="twintails", name="Twintails", usage_count=3))
    db.add(Tag(kind=TagKind.tag, slug="scale-1-7", name="1/7 scale", usage_count=2))
    db.add(Tag(kind=TagKind.character, slug="rem", name="Rem", usage_count=1))
    db.commit()

    # The bug: the term was applied to the page that came back rather than to
    # the table, so anything outside the most-used few hundred could not be
    # found however exactly it was typed.
    top = facet_tags(db, limit=60)
    check("the unaided list is the popular ones", len(top) == 60)
    check(
        "and the rare tag is nowhere in it",
        not any(t["slug"] == "twintails" for t in top),
    )

    found = facet_tags(db, limit=60, q="twintails")
    check("searching for it finds it", [t["slug"] for t in found] == ["twintails"], found)

    check(
        "matching is case-insensitive",
        [t["slug"] for t in facet_tags(db, limit=60, q="TWINTAILS")] == ["twintails"],
    )
    check(
        "and matches part of a name",
        any(t["slug"] == "twintails" for t in facet_tags(db, limit=60, q="wintai")),
    )
    check(
        "the slug works as well as the name",
        any(t["slug"] == "scale-1-7" for t in facet_tags(db, limit=60, q="scale-1-7")),
    )

    # A slash or a percent sign is an ordinary thing to type into a tag box.
    check(
        "a scale reads as text, not as a pattern",
        [t["slug"] for t in facet_tags(db, limit=60, q="1/7")] == ["scale-1-7"],
    )
    check(
        "and a wildcard matches nothing rather than everything",
        facet_tags(db, limit=60, q="%") == [],
    )
    check("an underscore likewise", facet_tags(db, limit=60, q="_") == [])

    # Kind and term still narrow together.
    check(
        "a kind filter still applies",
        [t["slug"] for t in facet_tags(db, limit=60, kinds=["character"], q="rem")] == ["rem"],
    )
    check(
        "and excludes matches of the wrong kind",
        facet_tags(db, limit=60, kinds=["character"], q="twintails") == [],
    )

    # Ranking is unchanged: the most used match comes first.
    ordered = facet_tags(db, limit=5, q="common")
    check(
        "matches are still most-used first",
        [t["usage_count"] for t in ordered] == sorted(
            [t["usage_count"] for t in ordered], reverse=True
        ),
        ordered,
    )

    db.query(Tag).delete()
    db.commit()
    db.close()


def test_price_history_follows_the_cheapest_copy() -> None:
    print("\n== The price line is the product's price, not one copy's ==")
    from app.db import SessionLocal, init_db, rebuild_price_aggregates
    from app.models import Condition, Item, Listing, ListingStatus, PricePoint
    from app.services import catalog

    init_db()
    db = SessionLocal()
    db.query(PricePoint).delete()
    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()

    # The real shape of the bug, from FIGURE-184067-R: nine graded copies,
    # cheapest at 34,380, dearest at 42,980. The product's asking price never
    # moved; the chart showed a step up to 42,980 all the same.
    item = Item(
        provider="amiami",
        code="FIGURE-184067-R",
        name="Shibuna 1/7",
        condition=Condition.preowned,
        currency="JPY",
        current_price=34_380,
        in_stock=True,
    )
    db.add(item)
    db.flush()

    started = datetime.now(timezone.utc) - timedelta(days=4)
    # Product level: the cheapest copy, twice, unchanged.
    for offset in (0, 1):
        db.add(
            PricePoint(
                item=item,
                recorded_at=started + timedelta(days=offset),
                price=34_380,
                currency="JPY",
                in_stock=True,
            )
        )
    # Per copy: the shelf-life sampler pricing individual graded copies.
    for code, price in (("R091", 42_980), ("R125", 38_680)):
        listing = Listing(
            item_id=item.id,
            code=f"FIGURE-184067-{code}",
            price=price,
            last_price=price,
            currency="JPY",
            status=ListingStatus.live,
            first_seen_at=started,
            last_seen_at=started,
        )
        db.add(listing)
        db.flush()
        db.add(
            PricePoint(
                item=item,
                listing=listing,
                recorded_at=started + timedelta(days=2),
                price=price,
                currency="JPY",
                in_stock=True,
            )
        )
    db.commit()

    points = catalog.history(db, item.id)
    check("the line is the product-level series", len(points) == 2, len(points))
    check(
        "so it never steps to a dearer grade",
        {p.price for p in points} == {34_380},
        sorted({p.price for p in points}),
    )
    check(
        "and the per-copy points are still stored",
        db.query(PricePoint).filter(PricePoint.listing_id.is_not(None)).count() == 2,
    )

    stats = catalog.price_stats(db, item.id)
    check("the highest seen is the product's, not a copy's", stats["highest"] == 34_380, stats)
    check("as is the lowest", stats["lowest"] == 34_380)
    check("the average too", stats["average"] == 34_380)
    check("and the count matches the line", stats["points"] == 2)
    check("tracked since the first product-level point", stats["tracked_since"] is not None)

    # A database that already mixed the two is corrected on upgrade.
    item.lowest_price = 34_380
    item.highest_price = 42_980
    item.average_price = 38_680
    db.commit()
    db.close()

    changed = rebuild_price_aggregates()
    check("the stored range is rebuilt", changed == 1, changed)

    db = SessionLocal()
    item = db.query(Item).filter_by(code="FIGURE-184067-R").one()
    check("the highest is the product's again", item.highest_price == 34_380, item.highest_price)
    check("the average follows", item.average_price == 34_380, item.average_price)
    check("and a second run finds nothing to do", rebuild_price_aggregates() == 0)

    db.query(PricePoint).delete()
    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()
    db.close()


def test_head_slice_reads_only_its_front() -> None:
    print("\n== A capped slice reads the front, an uncapped one reads it all ==")
    from app.db import SessionLocal, adopt_intake_ordering, init_db
    from app.models import CatalogCrawl
    from app.services.crawler import _page_limit

    swept = datetime.now(timezone.utc)
    full = CatalogCrawl(
        provider="amiami", scope="s", pages_total=213, head_pages=0,
        last_full_sweep_at=swept,
    )
    check("no short pass means the whole slice", _page_limit(full) == 213)

    head = CatalogCrawl(
        provider="amiami", scope="s", pages_total=213, head_pages=30,
        full_sweep_interval_minutes=1440, last_full_sweep_at=swept,
    )
    check("a short pass stops at the front", _page_limit(head) == 30)

    due = CatalogCrawl(
        provider="amiami", scope="s", pages_total=213, head_pages=30,
        full_sweep_interval_minutes=1440,
        last_full_sweep_at=swept - timedelta(days=2),
    )
    check("and the sweep still comes round", _page_limit(due) == 213)

    # --- what an existing installation gets ---------------------------------
    init_db()
    db = SessionLocal()
    db.query(CatalogCrawl).delete()
    db.commit()

    # As shipped before: hourly, on the registration ordering, and - if this
    # installation ran the release that split them - a second row for the
    # front pages, counting itself against the whole catalogue.
    db.add(
        CatalogCrawl(
            provider="amiami",
            scope="figures_preowned",
            label="Pre-owned figures",
            query={"category_id": 1, "condition": "preowned", "sort": "newest"},
            recheck_interval_minutes=60,
            cursor_page=88,
        )
    )
    db.add(
        CatalogCrawl(
            provider="amiami",
            scope="figures_preowned_head",
            label="Pre-owned, newest first",
            query={"category_id": 1, "condition": "preowned", "sort": "updated"},
            recheck_interval_minutes=30,
        )
    )
    db.commit()
    db.close()

    check("the slice is moved over", adopt_intake_ordering() > 0)

    db = SessionLocal()
    check(
        "the split-out row is gone",
        db.query(CatalogCrawl).filter_by(scope="figures_preowned_head").count() == 0,
    )
    row = db.query(CatalogCrawl).filter_by(scope="figures_preowned").one()
    check("onto the ordering that tracks intake", row.query["sort"] == "updated", row.query)
    check("with a short pass of its own", row.head_pages == 30, row.head_pages)
    check("run hourly", row.recheck_interval_minutes == 60, row.recheck_interval_minutes)
    check("and a full sweep daily", row.full_sweep_interval_minutes == 1440)
    check(
        "a pass ordered the old way restarts rather than resuming",
        row.cursor_page == 1,
        row.cursor_page,
    )
    check("a second upgrade changes nothing", adopt_intake_ordering() == 0)

    # The other three have no front worth re-reading and nothing that moves
    # within the hour, so they are eased at the same time.
    db.close()
    db = SessionLocal()
    for scope, shipped, wanted in (
        ("figures_in_stock", 60, 1440),
        ("figures_preorder", 180, 1440),
        ("figures_all", 10080, 20160),
    ):
        db.add(
            CatalogCrawl(
                provider="amiami",
                scope=scope,
                query={"category_id": 1, "sort": "newest"},
                recheck_interval_minutes=shipped,
                head_pages=20,
            )
        )
    db.commit()
    db.close()
    adopt_intake_ordering()
    db = SessionLocal()
    for scope, wanted in (
        ("figures_in_stock", 1440),
        ("figures_preorder", 1440),
        ("figures_all", 20160),
    ):
        row = db.query(CatalogCrawl).filter_by(scope=scope).one()
        check(f"{scope} is eased to {wanted} min", row.recheck_interval_minutes == wanted,
              row.recheck_interval_minutes)
        check(f"{scope} reads all of itself", row.head_pages == 0, row.head_pages)

    # An interval someone chose is theirs, and so is a sort they picked.
    # Named explicitly: the loop above left "row" on the last quiet slice.
    row = db.query(CatalogCrawl).filter_by(scope="figures_preowned").one()
    row.recheck_interval_minutes = 240
    row.query = {"category_id": 1, "condition": "preowned", "sort": "newest"}
    db.commit()
    db.close()
    adopt_intake_ordering()
    db = SessionLocal()
    row = db.query(CatalogCrawl).filter_by(scope="figures_preowned").one()
    check(
        "a hand-set interval survives the move",
        row.recheck_interval_minutes == 240,
        row.recheck_interval_minutes,
    )

    db.query(CatalogCrawl).delete()
    db.commit()
    db.close()


def test_copy_codes_are_not_product_codes() -> None:
    print("\n== A copy and the product it belongs to are addressed differently ==")
    from app.providers.amiami import is_copy_code

    # FIGURE-184067-R is the product; FIGURE-184067-R124 is the 124th used
    # copy of it. Asked for a copy under "gcode" the API answers that it has
    # no such item, and asked for a product under "scode" it rejects the
    # request - so neither substitutes for the other in either direction.
    for code in ("FIGURE-184067-R124", "FIGURE-012382-R99", "FIGURE-200001-R1"):
        check(f"{code} is one copy", is_copy_code(code))
    for code in ("FIGURE-184067-R", "FIGURE-184067", "FIG-MOE-1524-R", "HOB-FIG-2390-R"):
        check(f"{code} is a product", not is_copy_code(code))
    check("nothing is not a copy", not is_copy_code(""))


def test_shop_links_use_the_right_key() -> None:
    print("\n== Links out to the shop carry the key that resolves ==")
    import re

    # The rule the frontend applies, restated here so a change to one without
    # the other is caught. Every link out of the buying-choices list and the
    # shelf-life table used gcode for both, so none of them resolved.
    def shop_url(code: str) -> str:
        key = "scode" if re.search(r"-R\d+$", code) else "gcode"
        return f"https://www.amiami.com/eng/detail/?{key}={code}"

    check(
        "a copy links by scode",
        "scode=FIGURE-184067-R124" in shop_url("FIGURE-184067-R124"),
        shop_url("FIGURE-184067-R124"),
    )
    check(
        "a product links by gcode",
        "gcode=FIGURE-184067-R" in shop_url("FIGURE-184067-R"),
        shop_url("FIGURE-184067-R"),
    )


def test_a_watch_waiting_is_not_a_watch_failing() -> None:
    print("\n== A watch on something out of stock is not broken ==")
    from app.db import SessionLocal, init_db
    from app.models import User, Watch, WatchKind
    from app.services import health

    init_db()
    db = SessionLocal()
    db.query(Watch).delete()
    db.commit()
    user = db.query(User).first()
    if user is None:
        user = User(email="recap@example.com", password_hash="x", is_active=True)
        db.add(user)
        db.flush()

    # AmiAmi has no listing for a used figure until somebody sells one back,
    # which is precisely what this watch is waiting for. It used to count as
    # an error, reach five, and be reported as failing every single morning.
    waiting = Watch(
        user_id=user.id,
        kind=WatchKind.item,
        provider="amiami",
        label="Waiting for a copy",
        item_code="FIGURE-999999-R",
        enabled=True,
        consecutive_errors=40,
        last_success_at=datetime.now(timezone.utc) - timedelta(days=3),
    )
    # One that has never resolved is a different matter: a mistyped code does
    # not fix itself, and nothing else would ever say so.
    never = Watch(
        user_id=user.id,
        kind=WatchKind.item,
        provider="amiami",
        label="Typo",
        item_code="FIGURE-NOT-A-CODE",
        enabled=True,
        consecutive_errors=6,
        last_success_at=None,
    )
    db.add_all([waiting, never])
    db.commit()

    issues = {i.key: i for i in health.collect_issues(db)}
    reported = issues.get("watches:failing")
    check("something is reported", reported is not None, sorted(issues))
    if reported:
        check(
            "the never-resolved one is named",
            "Typo" in reported.detail,
            reported.detail,
        )
        check(
            "and the waiting one is not",
            "Waiting for a copy" not in reported.detail,
            reported.detail,
        )
        check("only one is counted", "1 watch" in reported.title, reported.title)

    db.query(Watch).delete()
    db.commit()
    db.close()


def test_linking_estimate_describes_the_job_that_runs() -> None:
    print("\n== The cross-reference estimate matches the job doing the work ==")
    from app.config import settings
    from app.services.enrich import throughput_per_minute

    # The setting is 0 by default, meaning "work it out from the rate and the
    # interval". The estimate read that raw value, so max(1, 0) described one
    # item every five minutes against a job doing twenty-five - and quoted 203
    # days where the real figure was near eight.
    check("the raw setting is a sentinel", settings.mfc_batch_size == 0)
    check(
        "and the effective size is far larger",
        settings.mfc_effective_batch_size > 1,
        settings.mfc_effective_batch_size,
    )

    rate = throughput_per_minute()
    check("throughput is reported in items", rate > 0, rate)
    check(
        "and never claims more than the request budget allows",
        rate <= settings.mfc_requests_per_minute,
        (rate, settings.mfc_requests_per_minute),
    )


def test_daily_recap_counts_copies_not_products() -> None:
    print("\n== The daily recap counts individual copies ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item, Listing, ListingOutcome, ListingStatus
    from app.services.shelflife import daily_recap

    init_db()
    db = SessionLocal()
    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()

    # One product, several copies moving under it. A product-level count would
    # see nothing happen here at all.
    item = Item(provider="amiami", code="R-1", name="Used figure",
                condition=Condition.preowned)
    db.add(item)
    db.flush()

    now = datetime.now(timezone.utc)
    for n in range(5):
        db.add(Listing(item_id=item.id, code=f"R-1-A{n}", currency="JPY",
                       status=ListingStatus.live, first_seen_at=now - timedelta(days=1),
                       last_seen_at=now))
    for n in range(3):
        db.add(Listing(item_id=item.id, code=f"R-1-S{n}", currency="JPY",
                       status=ListingStatus.gone, first_seen_at=now - timedelta(days=6),
                       last_seen_at=now - timedelta(days=1),
                       vanished_before=now - timedelta(days=1),
                       outcome=ListingOutcome.sold))
    db.add(Listing(item_id=item.id, code="R-1-W0", currency="JPY",
                   status=ListingStatus.gone, first_seen_at=now - timedelta(days=6),
                   last_seen_at=now - timedelta(days=1),
                   vanished_before=now - timedelta(days=1),
                   outcome=ListingOutcome.withdrawn))
    db.commit()

    out = daily_recap(db, days=4)
    by_date = {row["date"]: row for row in out["days"]}
    yesterday = (now - timedelta(days=1)).date().isoformat()
    row = by_date[yesterday]

    check("arrivals are counted per copy", row["arrived"] == 5, row)
    check("sales are counted per copy", row["sold"] == 3, row)
    check("a withdrawal is not called a sale", row["withdrawn"] == 1, row)
    check("departures are the sum of both", row["gone"] == 4, row)
    check("and the net is the difference", row["net"] == 1, row)

    check("live copies are counted", out["live_listings"] == 5, out["live_listings"])
    check("every day in the window is present", len(out["days"]) == 4, len(out["days"]))
    check(
        "quiet days read as quiet rather than missing",
        all(d["arrived"] == 0 for date, d in by_date.items() if date != yesterday),
    )
    check("newest day first", out["days"][0]["date"] > out["days"][-1]["date"])

    # Today is still happening, so averaging it against whole days would make
    # every morning look like a collapse.
    today = now.date().isoformat()
    complete = [d for d in out["days"] if d["date"] != today]
    check(
        "the typical figure excludes today",
        abs(out["typical_arrivals"] - sum(d["arrived"] for d in complete) / len(complete))
        < 0.05,
        out["typical_arrivals"],
    )

    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()
    db.close()


def test_condition_filter_asks_about_copies() -> None:
    print("\n== The condition filter asks whether a copy can be bought ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item, Listing, ListingStatus
    from app.schemas import LocalSearchRequest
    from app.services import localsearch

    init_db()
    db = SessionLocal()
    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()

    now = datetime.now(timezone.utc)

    # The real shape, from FIGURE-184067-R: nine copies, two B/B, six B+/B,
    # one A/B. The product-level grade is only ever the cheapest copy's, so
    # filtering on that would hide this product from anyone asking for an A.
    nine = Item(provider="amiami", code="P-1", name="Nine copies",
                condition=Condition.preowned, in_stock=True)
    db.add(nine)
    db.flush()
    for n, (item_grade, box_grade) in enumerate(
        [("B", "B"), ("B", "B")] + [("B+", "B")] * 6 + [("A", "B")]
    ):
        db.add(Listing(item_id=nine.id, code=f"P-1-R{n}", currency="JPY",
                       status=ListingStatus.live, item_grade=item_grade,
                       box_grade=box_grade, first_seen_at=now, last_seen_at=now))

    # And a product whose only good copy has already sold.
    sold = Item(provider="amiami", code="P-2", name="Sold the good one",
                condition=Condition.preowned, in_stock=True)
    db.add(sold)
    db.flush()
    db.add(Listing(item_id=sold.id, code="P-2-R1", currency="JPY",
                   status=ListingStatus.gone, item_grade="S", box_grade="S",
                   first_seen_at=now, last_seen_at=now))
    db.add(Listing(item_id=sold.id, code="P-2-R2", currency="JPY",
                   status=ListingStatus.live, item_grade="C", box_grade="D",
                   first_seen_at=now, last_seen_at=now))
    db.commit()

    def codes(**kw):
        return sorted(i.code for i in localsearch.search(db, LocalSearchRequest(**kw)).items)

    check("without a filter both are shown", codes() == ["P-1", "P-2"], codes())
    check(
        "asking for an A finds the product whose dear copy is one",
        codes(item_grade="A") == ["P-1"],
        codes(item_grade="A"),
    )
    check(
        "widening it means that grade or better",
        codes(item_grade="C", grade_or_better=True) == ["P-1", "P-2"],
        codes(item_grade="C", grade_or_better=True),
    )
    check(
        "a copy that has already sold does not qualify its product",
        codes(box_grade="S") == [],
        codes(box_grade="S"),
    )
    check(
        "both filters must hold for the same copy",
        codes(item_grade="A", box_grade="B") == ["P-1"],
        codes(item_grade="A", box_grade="B"),
    )
    # P-1's A copy has a B box; P-2's live copy is C/D. Asking for an A figure
    # in an A box matches neither, and must not match P-1 by pairing its A
    # figure with some other copy's box.
    check(
        "and never by pairing two different copies",
        codes(item_grade="A", box_grade="A") == [],
        codes(item_grade="A", box_grade="A"),
    )

    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()
    db.close()


def test_newly_listed_used_is_not_newly_known() -> None:
    print("\n== Newly on sale used is a different question from newly known ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item, Listing
    from app.schemas import LocalSearchRequest
    from app.services import localsearch

    init_db()
    db = SessionLocal()
    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()

    now = datetime.now(timezone.utc)
    # A figure known here for a year that took a copy in twenty minutes ago,
    # and one first seen yesterday that has had none since. AmiAmi's own
    # newest-first ordering answers neither question: it sorts by when the
    # product record was registered.
    old = Item(provider="amiami", code="OLD-PRODUCT", name="Known for a year",
               condition=Condition.preowned, in_stock=True,
               first_seen_at=now - timedelta(days=365),
               last_listing_at=now - timedelta(minutes=20))
    fresh = Item(provider="amiami", code="NEW-PRODUCT", name="Seen yesterday",
                 condition=Condition.preowned, in_stock=True,
                 first_seen_at=now - timedelta(days=1),
                 last_listing_at=now - timedelta(days=1))
    never = Item(provider="amiami", code="NO-COPIES", name="Never had one",
                 condition=Condition.preowned, in_stock=True,
                 first_seen_at=now - timedelta(days=2), last_listing_at=None)
    db.add_all([old, fresh, never])
    db.commit()

    def codes(sort):
        return [i.code for i in localsearch.search(db, LocalSearchRequest(sort=sort)).items]

    check(
        "newest here goes by when the product appeared",
        codes("newest")[0] == "NEW-PRODUCT",
        codes("newest"),
    )
    check(
        "newly listed used goes by when a copy did",
        codes("newest_copy")[0] == "OLD-PRODUCT",
        codes("newest_copy"),
    )
    check(
        "a product that never had a copy sorts last rather than looking fresh",
        codes("newest_copy")[-1] == "NO-COPIES",
        codes("newest_copy"),
    )

    db.query(Item).delete()
    db.commit()
    db.close()


def test_condition_notes_are_told_from_shipping_notices() -> None:
    print("\n== The red text that explains a low price, and the red text that does not ==")
    import json
    import pathlib as _pathlib

    from app.providers.amiami import condition_note, shop_notes

    def red(text: str) -> str:
        return f"<font color=red><b>{text}</b></font>"

    # Every one of these was found in AmiAmi's own data while sampling the
    # pre-owned catalogue (.probe/remarks_survey.py). Nineteen red passages
    # turned up across a hundred-odd listings, in three kinds.
    kept = {
        "[Discoloration] Upper body skin area has become white\n"
        "Both legs are sticky and have stains":
            "[Discoloration] Upper body skin area has become white · "
            "Both legs are sticky and have stains",
        "The pencil board is missing.": "The pencil board is missing.",
        "The decal is missing.": "The decal is missing.",
        "White area on the skirt has become yellowish":
            "White area on the skirt has become yellowish",
        "Outfit is sticky and has droplets due to age":
            "Outfit is sticky and has droplets due to age",
        "Blister has become yellowish\nThere are stains on the inner cardboard":
            "Blister has become yellowish · There are stains on the inner cardboard",
        "[Missing] Shoes": "[Missing] Shoes",
    }
    for text, expected in kept.items():
        got = condition_note(red(text))
        check(f"kept: {text.splitlines()[0][:44]}", got == expected, got)

    # A bonus is kept too. It is the same kind of thing as a fault - something
    # the shop says about this copy that exists on its page and nowhere else -
    # and only the reading differs, which is what the tag is for.
    check(
        "a bonus is kept",
        condition_note(red("Poster is included.")) == "Poster is included.",
    )
    check(
        "and marked as one rather than as a fault",
        shop_notes(red("Poster is included.")) == [
            {"text": "Poster is included.", "kind": "bonus"}
        ],
        shop_notes(red("Poster is included.")),
    )
    check(
        "while a fault is marked as a fault",
        shop_notes(red("The decal is missing."))[0]["kind"] == "fault",
    )

    # Only the shipping sentence goes, and it is the reason any filtering
    # happens: eleven of nineteen sampled red passages were this one line,
    # which every oversized item carries.
    shipping = (
        "*Shipping costs for this item may be very high due to package size or/and weight."
    )
    check("a shipping notice is dropped", condition_note(red(shipping)) is None)

    # The case that decides the design. These arrive in one red block, so a
    # verdict on the block would either drop the postcard with the shipping
    # line or keep the shipping line with the postcard.
    mixed = condition_note(red(f"Postcard is included\n{shipping}"))
    check("a bonus survives a shipping line beside it", mixed == "Postcard is included", mixed)

    both = shop_notes(red("Postcard is included\nRight rabbit ear is detached"))
    check(
        "a bonus and a fault together are kept apart",
        [n["kind"] for n in both] == ["bonus", "fault"],
        both,
    )
    check("with both texts intact", [n["text"] for n in both] == [
        "Postcard is included", "Right rabbit ear is detached"
    ], both)

    check("the markup does not survive", "<" not in (kept and mixed or ""), mixed)
    check("nothing at all stays nothing", condition_note(None) is None)
    check("an empty red tag too", condition_note("<font color=red></font>") is None)
    check("and plain prose is not red text", condition_note("Sculptor: Makio") is None)

    # Unfamiliar wording is kept rather than discarded. A defect that goes
    # unshown is the failure that matters; a stray notice is only noise.
    check(
        "unrecognised red text is kept",
        condition_note(red("Left arm has a repair mark")) == "Left arm has a repair mark",
    )

    # An earlier version keyed on a bracketed category like "[Discoloration]".
    # Of the nineteen real passages, none outside the original example had one.
    check(
        "a note without a bracketed category still counts",
        condition_note(red("Right hand is discoloured")) is not None,
    )

    # When the sampler has left raw captures behind, run the real bytes
    # through as well - separators included, which is what a collapsed
    # transcript cannot tell you.
    captured = _pathlib.Path(__file__).resolve().parents[2] / ".probe" / "red-remarks.json"
    if captured.exists():
        rows = json.loads(captured.read_text(encoding="utf-8"))
        notes = [(r["gcode"], condition_note(r["remarks"])) for r in rows]
        leaked = [(code, note) for code, note in notes if note and "shipping cost" in note.lower()]
        check(
            f"no shipping notice leaks through {len(rows)} real captures",
            not leaked,
            leaked[:3],
        )
        tagged = [n for r in rows for n in shop_notes(r["remarks"])]
        check(
            "and every real statement is tagged one way or the other",
            all(n["kind"] in ("fault", "bonus") for n in tagged),
            {n["kind"] for n in tagged},
        )
        check(
            "and the markup never does either",
            not [n for _, n in notes if n and ("<" in n or ">" in n)],
        )


def test_grades_match_exactly_unless_widened() -> None:
    print("\n== A condition filter matches that grade, not everything above it ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item, Listing, ListingStatus
    from app.schemas import LocalSearchRequest
    from app.services import localsearch

    init_db()
    db = SessionLocal()
    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()

    now = datetime.now(timezone.utc)
    for code, grades in (
        ("CHEAP-C", [("C", "B")]),
        ("MINT-A", [("A", "A")]),
        ("MIXED", [("C", "C"), ("A", "B")]),
    ):
        item = Item(provider="amiami", code=code, name=code,
                    condition=Condition.preowned, in_stock=True)
        db.add(item)
        db.flush()
        for n, (item_grade, box_grade) in enumerate(grades):
            db.add(Listing(item_id=item.id, code=f"{code}-R{n}", currency="JPY",
                           status=ListingStatus.live, item_grade=item_grade,
                           box_grade=box_grade, first_seen_at=now, last_seen_at=now))
    db.commit()

    def codes(**kw):
        return sorted(i.code for i in localsearch.search(db, LocalSearchRequest(**kw)).items)

    # Asking for C is asking for the cheap ones. As a floor it would return
    # the mint copy too, which is the opposite of what was wanted.
    check("exactly C leaves the mint one out", codes(item_grade="C") == ["CHEAP-C", "MIXED"],
          codes(item_grade="C"))
    check(
        "and widening it brings that one in",
        codes(item_grade="C", grade_or_better=True) == ["CHEAP-C", "MINT-A", "MIXED"],
        codes(item_grade="C", grade_or_better=True),
    )
    check("exactly A finds the A copies", codes(item_grade="A") == ["MINT-A", "MIXED"],
          codes(item_grade="A"))
    check("D matches nothing here", codes(item_grade="D") == [], codes(item_grade="D"))
    check("no grade asked for means no filtering", len(codes()) == 3)

    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()
    db.close()


def test_shop_notes_hold_up_on_wording_never_seen() -> None:
    print("\n== Wordings the sampler never turned up ==")
    from app.providers.amiami import shop_notes

    def red(text: str) -> str:
        return f"<font color=red><b>{text}</b></font>"

    def kind(text: str) -> str:
        notes = shop_notes(red(text))
        return notes[0]["kind"] if notes else "dropped"

    # None of these appeared in the sample. The first version of the rules got
    # six of the fifteen wrong: it knew only "X is included" as a bonus and a
    # fixed list of shipping phrases, so a bonus worded any other way went
    # under a warning triangle and a new shipping sentence went with it.
    cases = [
        # A bonus, said half a dozen ways.
        ("Includes a bonus postcard.", "bonus"),
        ("Comes with a tapestry.", "bonus"),
        ("Bonus item included", "bonus"),
        ("A postcard is included with this item.", "bonus"),
        ("Tapestry is included", "bonus"),
        # Negations are the opposite of a bonus: a part that should be there
        # and is not. Reading "is not included" as a bonus would be the worst
        # of the failures - a missing piece announced as an extra.
        ("The stand is not included.", "fault"),
        ("Bonus poster is not included.", "fault"),
        ("The base is missing.", "fault"),
        # Faults in wording nothing has taught it.
        ("Left hand is broken", "fault"),
        ("Slight paint chipping on the base", "fault"),
        ("Hair parts have warped slightly", "fault"),
        # Shop-wide notices. AmiAmi marks these with an asterisk - across the
        # sampled listings every asterisked line was one and no line about a
        # copy carried one - so the mark generalises past any phrase list.
        ("*This item cannot be shipped by air.", "dropped"),
        ("*Please note delivery may take longer.", "dropped"),
        ("*Shipping costs for this item may be very high.", "dropped"),
        ("\u203bThis item ships separately.", "dropped"),
        # And the safety net: a fault does not stop being one because the shop
        # happened to mark it. Losing a fault is the expensive mistake.
        ("*The box has a large dent on one corner.", "fault"),
        ("*Please note the figure has a scratch on its base.", "fault"),
    ]

    wrong = [(text, want, kind(text)) for text, want in cases if kind(text) != want]
    for text, want, got in wrong:
        check(f"{want} -> {got}: {text[:44]}", False, got)
    check(
        f"all {len(cases)} unseen wordings are read correctly",
        not wrong,
        f"{len(wrong)} wrong",
    )


def test_a_note_belongs_to_one_copy() -> None:
    print("\n== A condition note describes one copy, not the product ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item, Listing, ListingStatus
    from app.providers.amiami import AmiAmiProvider
    from app.services import shelflife

    # The real shape, from FIGURE-045661-R. Asked about R514 the shop returns
    # a C-grade at 3,920 with "[Discoloration] Upper body skin area has become
    # white"; asked about R515 it returns an A-grade at 9,780 with an empty
    # remarks field. Filing that note against the product would put the first
    # copy's stains on the second copy's listing.
    raw = {
        "gcode": "FIGURE-045661-R",
        "scode": "FIGURE-045661-R514",
        "price": 3920,
        "sname": "(Pre-owned ITEM:C/BOX:B)Azur Lane Atago Summer March Ver. 1/7",
        "remarks": (
            "<font color=red><b>[Discoloration] Upper body skin area has become white\n"
            "Both legs are sticky and have stains</b></font>"
        ),
    }
    embedded = {
        "other_items": [
            {"scode": "FIGURE-045661-R515", "price": 9780, "condition": "Condition Item:A　Box:B"},
            {"scode": "FIGURE-045661-R513", "price": 5880, "condition": "Condition Item:B　Box:B"},
        ]
    }

    variants = AmiAmiProvider._collect_variants(raw, embedded)
    by_code = {v["code"]: v for v in variants}

    check("every copy is collected", len(variants) == 3, sorted(by_code))
    check(
        "the note is on the copy the shop answered about",
        by_code["FIGURE-045661-R514"]["note"] is not None,
        by_code["FIGURE-045661-R514"]["note"],
    )
    check(
        "and says what it says",
        "Discoloration" in by_code["FIGURE-045661-R514"]["note"],
    )
    check(
        "the A-grade copy carries nothing",
        by_code["FIGURE-045661-R515"]["note"] is None,
        by_code["FIGURE-045661-R515"]["note"],
    )
    check(
        "nor does the B-grade one",
        by_code["FIGURE-045661-R513"]["note"] is None,
    )

    # Which copy the shop answers with is its own choice and not always the
    # cheapest - FIGURE-184067-R answers with a 38,680 copy while the cheapest
    # is 34,380 - so the note follows the scode rather than a position.
    provider = AmiAmiProvider()
    normalized = provider._normalize_detail(raw, embedded)
    check(
        "the product records which copy its note came from",
        normalized.condition_note_code == "FIGURE-045661-R514",
        normalized.condition_note_code,
    )
    check(
        "and the cheapest copy is still the headline price",
        normalized.price == 3920,
        normalized.price,
    )

    # And it reaches the stored copy, not the product row.
    init_db()
    db = SessionLocal()
    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()

    item = Item(provider="amiami", code="FIGURE-045661-R", name="Atago",
                condition=Condition.preowned, in_stock=True, currency="JPY")
    db.add(item)
    db.flush()
    shelflife.reconcile(db, item, variants, observed_at=datetime.now(timezone.utc))
    db.commit()

    stored = {row.code: row for row in db.query(Listing).all()}
    check("a copy exists for each variant", len(stored) == 3, sorted(stored))
    check(
        "the marked-down copy carries the note",
        stored["FIGURE-045661-R514"].condition_note is not None,
    )
    check(
        "and the others carry none",
        all(stored[c].condition_note is None
            for c in ("FIGURE-045661-R515", "FIGURE-045661-R513")),
        {c: stored[c].condition_note for c in stored},
    )

    # A later pass that says nothing about a copy must not erase what an
    # earlier one recorded: the shop returns no remarks field for copies it
    # was not asked about, and silence is not a retraction.
    quiet = [dict(v, note=None) for v in variants]
    shelflife.reconcile(db, item, quiet, observed_at=datetime.now(timezone.utc))
    db.commit()
    db.expire_all()
    again = db.query(Listing).filter_by(code="FIGURE-045661-R514").one()
    check("a silent pass does not clear it", again.condition_note is not None,
          again.condition_note)

    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()
    db.close()


def test_each_copy_carries_its_own_price_trail() -> None:
    print("\n== A copy's price over time, for the copy and no other ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item, Listing, ListingStatus, PricePoint
    from app.services import shelflife

    init_db()
    db = SessionLocal()
    db.query(PricePoint).delete()
    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()

    now = datetime.now(timezone.utc)
    item = Item(provider="amiami", code="T-1", name="Trail",
                condition=Condition.preowned, currency="JPY")
    db.add(item)
    db.flush()

    # AmiAmi marks a used copy down while it sits, a couple of hundred yen at
    # a time. This one has come down twice.
    moved = Listing(item_id=item.id, code="T-1-R161", currency="JPY",
                    status=ListingStatus.live, price=9980, last_price=9580,
                    first_seen_at=now - timedelta(days=20), last_seen_at=now)
    # And this one has not moved at all, which is the common case.
    still = Listing(item_id=item.id, code="T-1-R162", currency="JPY",
                    status=ListingStatus.live, price=7480, last_price=7480,
                    first_seen_at=now - timedelta(days=6), last_seen_at=now)
    db.add_all([moved, still])
    db.flush()
    for days, price in ((12, 9780), (4, 9580)):
        db.add(PricePoint(item=item, listing=moved, recorded_at=now - timedelta(days=days),
                          price=price, currency="JPY", in_stock=True))
    # A product-level point, which belongs to the product's own history and
    # must not turn up in any copy's trail.
    db.add(PricePoint(item=item, recorded_at=now - timedelta(days=15), price=7480,
                      currency="JPY", in_stock=True))
    db.commit()

    rows = {row["code"]: row for row in shelflife.summary(db, item)["listings"]}

    trail = rows["T-1-R161"]["price_trail"]
    check("the repriced copy has a trail", len(trail) == 3, len(trail))
    check(
        "opening at what it was listed for",
        trail[0]["price"] == 9980,
        trail[0]["price"],
    )
    check(
        "then each change in order",
        [p["price"] for p in trail] == [9980, 9780, 9580],
        [p["price"] for p in trail],
    )
    check("oldest first", trail[0]["at"] < trail[-1]["at"])

    # A point is only written when the figure moved, so the opening price is
    # not among them. Without putting it back a copy marked down once would
    # look as though it had only ever had the lower price.
    check(
        "the opening price is not lost",
        trail[0]["price"] != rows["T-1-R161"]["last_price"],
    )

    quiet = rows["T-1-R162"]["price_trail"]
    check("a copy that never moved has one entry", len(quiet) == 1, quiet)
    check("and that is its price", quiet[0]["price"] == 7480)

    # The product-level point sits at 7,480 too; if trails were keyed loosely
    # it would appear in the untouched copy's trail and invent a change.
    check(
        "a product-level point is not in any copy's trail",
        all(len(r["price_trail"]) <= 3 for r in rows.values()),
        {c: len(r["price_trail"]) for c, r in rows.items()},
    )

    db.query(PricePoint).delete()
    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()
    db.close()


def test_a_refresh_notices_a_copy_has_gone() -> None:
    print("\n== Refreshing an item settles which copies are still there ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item, Listing, ListingStatus, PricePoint
    from app.providers.base import NormalizedItem
    from app.services import catalog

    init_db()
    db = SessionLocal()
    db.query(PricePoint).delete()
    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()

    now = datetime.now(timezone.utc)

    def detail(variants, *, in_stock=True, closed=False) -> NormalizedItem:
        return NormalizedItem(
            provider="amiami",
            code="R-1",
            name="Used figure",
            url="https://www.amiami.com/eng/detail/?gcode=R-1",
            currency="JPY",
            price=variants[0]["price"] if variants else None,
            condition="preowned",
            in_stock=in_stock,
            order_closed=closed,
            detail_loaded=True,
            variants=variants,
        )

    three = [
        {"code": "R-1-R10", "price": 5880, "condition": "Item:B Box:B",
         "item_grade": "B", "box_grade": "B", "note": None},
        {"code": "R-1-R11", "price": 7480, "condition": "Item:A Box:B",
         "item_grade": "A", "box_grade": "B", "note": None},
        {"code": "R-1-R12", "price": 9780, "condition": "Item:A Box:A",
         "item_grade": "A", "box_grade": "A", "note": None},
    ]
    item, _ = catalog.upsert_item(db, detail(three))
    db.commit()
    live = lambda: db.query(Listing).filter_by(status=ListingStatus.live).count()  # noqa: E731
    check("all three copies are recorded", live() == 3, live())

    # One sells. A refresh is a detail fetch, and a detail fetch is the only
    # thing that can see which of them has gone.
    catalog.upsert_item(db, detail(three[1:]))
    db.commit()
    check("the sold copy is closed", live() == 2, live())
    gone = db.query(Listing).filter_by(code="R-1-R10").one()
    check("and recorded as sold", gone.outcome.value == "sold", gone.outcome)
    check("with a date it vanished before", gone.vanished_before is not None)

    # Then the last two go and the product has nothing buyable left. This used
    # to be discarded - an empty variant list meant reconcile never ran - so
    # the copies stayed on the shelf for ever, which is exactly the moment the
    # shelf-life figure is waiting for.
    catalog.upsert_item(db, detail([], in_stock=False, closed=True))
    db.commit()
    check("a sold-out product closes the rest", live() == 0, live())

    # But only when the shop agrees. If it says the product is in stock and
    # the response yielded no copies, that is far likelier a change in their
    # payload than a sell-out, and acting on it would close every copy of
    # every item in a single pass.
    db.query(Listing).delete()
    db.commit()
    db.expire_all()  # item.listings still holds the rows just deleted
    item2, _ = catalog.upsert_item(db, detail(three))
    db.commit()
    check("three copies again", live() == 3)
    catalog.upsert_item(db, detail([], in_stock=True))
    db.commit()
    check(
        "an unparseable response closes nothing",
        live() == 3,
        live(),
    )

    db.query(PricePoint).delete()
    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()
    db.close()


def test_a_short_pass_stops_at_its_own_edge() -> None:
    print("\n== A short pass ends after its pages; a sweep runs to the end ==")
    from app.models import CatalogCrawl
    from app.services.crawler import _complete_cycle, _page_limit, full_sweep_due

    now = datetime.now(timezone.utc)

    def slice_at(cursor: int, sweeping: bool) -> CatalogCrawl:
        return CatalogCrawl(
            provider="amiami", scope="s", pages_total=213, head_pages=30,
            cursor_page=cursor, sweeping_all=sweeping,
            full_sweep_interval_minutes=1440, last_full_sweep_at=now,
        )

    # The bug this covers: the kind of pass used to be inferred from the
    # cursor, on the reasoning that a cursor past the front meant a sweep was
    # already under way. A short pass reaching page 31 of a 30-page front
    # looks exactly the same, so at its own boundary the limit jumped from 30
    # to 213 and every short pass quietly read the whole slice - undoing the
    # saving it exists for.
    check("a short pass reads its front", _page_limit(slice_at(1, False)) == 30)
    check("and still at the last of them", _page_limit(slice_at(30, False)) == 30)
    check(
        "then stops rather than becoming a sweep",
        _page_limit(slice_at(31, False)) == 30,
        _page_limit(slice_at(31, False)),
    )
    check("a sweep reads past the front", _page_limit(slice_at(31, True)) == 213)
    check("all the way to the end", _page_limit(slice_at(213, True)) == 213)

    # A slice with no front reads everything, whatever the flag says.
    plain = CatalogCrawl(provider="amiami", scope="s", pages_total=59, head_pages=0,
                         cursor_page=40, sweeping_all=False)
    check("a slice with no front is always sweeping", _page_limit(plain) == 59)

    # Due-ness is now about the schedule alone, not about where the cursor is.
    stale = CatalogCrawl(
        provider="amiami", scope="s", pages_total=213, head_pages=30, cursor_page=1,
        full_sweep_interval_minutes=1440,
        last_full_sweep_at=now - timedelta(days=2),
    )
    check("an overdue slice owes a sweep", full_sweep_due(stale))
    fresh = CatalogCrawl(
        provider="amiami", scope="s", pages_total=213, head_pages=30, cursor_page=99,
        full_sweep_interval_minutes=1440, last_full_sweep_at=now,
    )
    check(
        "and a recent one does not, wherever its cursor sits",
        not full_sweep_due(fresh),
    )
    check("a slice that has never swept owes one", full_sweep_due(
        CatalogCrawl(provider="amiami", scope="s", head_pages=30, cursor_page=1)
    ))

    # Finishing a short pass must not count as having swept, or the sweep
    # would be postponed for ever by the passes that replace it.
    from app.db import SessionLocal, init_db
    from app.models import CatalogCrawl as Row

    init_db()
    db = SessionLocal()
    db.query(Row).delete()
    db.commit()
    row = Row(provider="amiami", scope="s", pages_total=213, head_pages=30,
              cursor_page=31, sweeping_all=False, full_sweep_interval_minutes=1440,
              last_full_sweep_at=now - timedelta(hours=2))
    db.add(row)
    db.commit()
    _complete_cycle(db, row)
    check(
        "a finished short pass does not count as a sweep",
        row.last_full_sweep_at < now - timedelta(hours=1),
        row.last_full_sweep_at,
    )
    check("and leaves the flag clear for the next one", row.sweeping_all is False)

    row.cursor_page = 214
    row.sweeping_all = True
    _complete_cycle(db, row)
    check("a finished sweep does count", row.last_full_sweep_at > now - timedelta(minutes=1))

    db.query(Row).delete()
    db.commit()
    db.close()


def test_pruning_survives_a_large_catalogue() -> None:
    print("\n== History pruning holds up at catalogue scale ==")
    from app.db import SessionLocal, init_db
    from app.models import Condition, Item, PricePoint
    from app.services import catalog

    init_db()
    db = SessionLocal()
    db.query(PricePoint).delete()
    db.query(Item).delete()
    db.commit()

    # The protected set is two to four ids per item. It used to be read into
    # Python and handed back as a bind parameter each, which SQLite refuses
    # past about thirty-two thousand: "too many SQL variables". Housekeeping
    # then failed on every run, silently - the scheduler logs and moves on -
    # so nothing was pruned, and the alert pruning behind it never ran either.
    ITEMS = 9_000
    old = datetime.now(timezone.utc) - timedelta(days=2000)
    items = [
        Item(provider="amiami", code=f"P-{n}", name=f"Figure {n}",
             condition=Condition.preowned, currency="JPY", current_price=1000 + n)
        for n in range(ITEMS)
    ]
    db.add_all(items)
    db.flush()
    points = []
    for item in items:
        for offset, price in ((10, 900), (20, 1000), (30, 1100)):
            points.append(PricePoint(item_id=item.id, recorded_at=old - timedelta(days=offset),
                                     price=price, currency="JPY", in_stock=True))
    db.add_all(points)
    db.commit()

    before = db.query(PricePoint).count()
    check("a catalogue is in place", before == ITEMS * 3, before)

    deleted = catalog.prune_history(db, retention_days=365)
    check("pruning runs rather than raising", deleted > 0, deleted)

    # And it still protects what it promised: the cheapest, the dearest, the
    # first and the last of each item's series.
    remaining = db.query(PricePoint).count()
    check("the middle of each series went", remaining == before - deleted)
    check(
        "and every item keeps its extremes",
        remaining >= ITEMS * 2,
        remaining,
    )

    db.query(PricePoint).delete()
    db.query(Item).delete()
    db.commit()
    db.close()


def test_a_recovered_breaker_stops_reporting_itself_as_open() -> None:
    print("\n== A circuit that has served its backoff says so ==")
    import time as _time

    from app.providers.ratelimit import CircuitBreaker, CircuitOpen

    breaker = CircuitBreaker(threshold=2, reset_after=0.3)
    check("a fresh breaker is closed", not breaker.is_open)
    breaker.check()  # must not raise

    breaker.record_failure()
    check("one failure is not enough", not breaker.is_open)
    breaker.record_failure()
    check("the threshold trips it", breaker.is_open)
    check("and the snapshot agrees", breaker.snapshot()["open"])

    raised = False
    try:
        breaker.check()
    except CircuitOpen:
        raised = True
    check("requests are refused while it is open", raised)

    _time.sleep(0.35)

    # The bug this covers: is_open used to clear the trip as a side effect, so
    # the breaker only noticed it had recovered when something happened to ask
    # - and snapshot did not ask. The health check reads the snapshot and
    # makes no request of its own, so it went on raising an urgent "the shop
    # is refusing requests" alert after the backoff had elapsed. With every
    # slice resting there was nothing to ask on the shop's behalf, so the
    # alert repeated every quarter of an hour about a shop that was answering
    # perfectly well.
    check(
        "once the backoff elapses the snapshot says closed",
        not breaker.snapshot()["open"],
        breaker.snapshot(),
    )
    check("without anything having to ask first", not breaker.is_open)
    check("and it says how long is left", breaker.snapshot()["retry_in_seconds"] == 0.0)

    breaker.check()  # the probe is let through
    breaker.record_success()
    check("a success clears the count", breaker.snapshot()["failures"] == 0)
    check("and the backoff", breaker.snapshot()["backoff_seconds"] == 0.0)

    # Reading the state must not change it, however often it is read.
    breaker.record_failure()
    breaker.record_failure()
    for _ in range(5):
        breaker.snapshot()
        _ = breaker.is_open
    check("repeated reads leave it open", breaker.is_open)

    # A failed probe backs off further rather than starting over.
    first = breaker.snapshot()["backoff_seconds"]
    _time.sleep(0.35)
    breaker.record_failure()
    check(
        "a failure after recovery backs off longer",
        breaker.snapshot()["backoff_seconds"] > first,
        (first, breaker.snapshot()["backoff_seconds"]),
    )


def test_missing_exchange_rates_are_reported() -> None:
    print("\n== Having no exchange rate at all is worth saying out loud ==")
    from app.config import settings
    from app.db import SessionLocal, init_db
    from app.models import FxRate
    from app.services import fx, health

    init_db()
    db = SessionLocal()
    db.query(FxRate).delete()
    db.commit()

    # Stale rates were reported; no rates at all were not, and that is the
    # worse case. Every conversion returns nothing, so the landed price
    # disappears from the interface entirely - which reads as a display quirk
    # rather than as an instance that never reached either rate source.
    check("nothing converts without a rate", fx.convert(db, 3920, "JPY", "EUR") is None)
    keys = {issue.key for issue in health.collect_issues(db)}
    check("and that is now reported", "fx:missing" in keys, sorted(keys))
    urgent = [i for i in health.collect_issues(db) if i.key == "fx:missing"]
    check("as something to act on", urgent and urgent[0].urgent)

    # With a rate in place it goes quiet again.
    base = settings.fx_base_currency.upper()
    db.add(FxRate(base=base, quote="EUR", rate=0.0059, source="test"))
    db.commit()
    keys = {issue.key for issue in health.collect_issues(db)}
    check("a fetched rate clears it", "fx:missing" not in keys, sorted(keys))
    check("and nothing calls it stale either", "fx:stale" not in keys)
    check(
        "conversion works again",
        abs((fx.convert(db, 10_000, base, "EUR") or 0) - 59) < 0.5,
        fx.convert(db, 10_000, base, "EUR"),
    )

    # An old rate is still reported, separately, because a drifting price is a
    # different problem from an absent one.
    row = db.query(FxRate).filter_by(base=base, quote="EUR").one()
    row.fetched_at = datetime.now(timezone.utc) - timedelta(days=30)
    db.commit()
    keys = {issue.key for issue in health.collect_issues(db)}
    check("a month-old rate is called stale", "fx:stale" in keys, sorted(keys))
    check("and not called missing", "fx:missing" not in keys)

    db.query(FxRate).delete()
    db.commit()
    db.close()


def test_the_photo_queue_is_reached_however_full_the_cache_is() -> None:
    print("\n== The photo queue is asked for, not searched for ==")
    from app.db import SessionLocal, init_db
    from app.models import CachedImage, Condition, Item
    from app.services import images

    init_db()
    db = SessionLocal()
    db.query(CachedImage).delete()
    db.query(Item).delete()
    db.commit()

    now = datetime.now(timezone.utc)
    COUNT = 600
    items = [
        Item(provider="amiami", code=f"P-{n}", name=f"Figure {n}",
             condition=Condition.preowned, in_stock=True, currency="JPY",
             first_seen_at=now - timedelta(minutes=n),
             image_url=f"https://img.amiami.com/p{n}.jpg")
        for n in range(COUNT)
    ]
    db.add_all(items)
    db.flush()
    for item in items:
        images.register(db, images.urls_for_item(item), item_id=item.id)
    db.commit()

    check("every photo is linked to its product",
          db.query(CachedImage).filter(CachedImage.item_id.is_(None)).count() == 0)

    downloaded: list[str] = []
    original = images._download
    images._download = lambda url: (downloaded.append(url), (b"x" * 512, "image/jpeg"))[1]

    try:
        # Mark all but the last twenty as done - the shape a real cache takes,
        # since the newest are fetched first. The version before this walked
        # items to find the outstanding ones and gave up after a fixed number
        # of them, so once the finished prefix grew past that limit it reached
        # nothing at all: measured on forty thousand items it fetched fifty at
        # forty per cent coverage and zero from fifty per cent on.
        rows = db.query(CachedImage).order_by(CachedImage.id).all()
        for row in rows[:-20]:
            row.fetched_at = now - timedelta(hours=1)
            row.bytes = 512
        db.commit()

        check("only a tail is outstanding", images.pending_count(db) == 20)
        result = images.prefetch(db, limit=10)
        check("the prefetcher still reaches it", result["fetched"] == 10, result)
        check("and says what is left", result["queued"] == 10, result)

        # Order: what cannot be replaced comes first.
        db.query(CachedImage).update({"fetched_at": None, "bytes": 0})
        db.commit()
        db.expire_all()
        queue = images._pending_queue(db, 5)
        oldest_first = [q.item_id for q in queue]
        newest = [i.id for i in items[:5]]
        check(
            "used copies come newest first",
            oldest_first == newest,
            (oldest_first, newest),
        )
    finally:
        images._download = original

    db.query(CachedImage).delete()
    db.query(Item).delete()
    db.commit()
    db.close()


def test_waits_are_quoted_from_what_was_measured() -> None:
    print("\n== The waits come from what happened, not from the settings ==")
    from app.db import SessionLocal, init_db
    from app.models import CachedImage, Condition, Item
    from app.services import enrich, images

    init_db()
    db = SessionLocal()
    db.query(CachedImage).delete()
    db.query(Item).delete()
    db.commit()

    now = datetime.now(timezone.utc)

    # Photos: 48 landed over the last day, 100 still owed.
    for n in range(48):
        db.add(CachedImage(key=f"done-{n}", source_url=f"https://img/{n}.jpg",
                           kind="thumb", bytes=1000,
                           fetched_at=now - timedelta(hours=n % 24)))
    for n in range(100):
        db.add(CachedImage(key=f"owed-{n}", source_url=f"https://img/owed{n}.jpg",
                           kind="thumb"))
    db.commit()

    rate = images.download_rate(db)
    check("the measured rate counts what landed", rate == 2.0, rate)
    stats = images.stats(db)
    check("the queue is what is owed", stats["pending"] == 100, stats["pending"])
    check("and the wait uses the measured rate", stats["queue_hours"] == 50.0,
          stats["queue_hours"])
    check("which is flagged as measured", stats["queue_measured"])

    # With nothing landing, the settings must not be used to invent a figure:
    # a queue nothing is draining is stalled, not slow.
    db.query(CachedImage).filter(CachedImage.fetched_at.is_not(None)).delete()
    db.commit()
    stats = images.stats(db)
    check("no downloads means no measured rate", stats["measured_per_hour"] == 0.0)
    check("and the wait is not called measured", not stats["queue_measured"])

    # The linker, the same way.
    for n in range(24):
        db.add(Item(provider="amiami", code=f"L-{n}", name=f"Linked {n}",
                    condition=Condition.preowned, currency="JPY",
                    mfc_id=1000 + n, mfc_fetched_at=now - timedelta(hours=n)))
    for n in range(240):
        db.add(Item(provider="amiami", code=f"U-{n}", name=f"Unlinked {n}",
                    condition=Condition.preowned, currency="JPY"))
    db.commit()

    check("the linker's rate is measured too", enrich.measured_per_hour(db) == 1.0,
          enrich.measured_per_hour(db))
    seconds = enrich.eta_from_measurement(db)
    check("and 240 left at one an hour is ten days",
          seconds and abs(seconds / 86400 - 10) < 0.2, seconds)

    # Nothing looked up means no honest estimate rather than an invented one.
    db.query(Item).filter(Item.mfc_fetched_at.is_not(None)).delete()
    db.commit()
    check("no lookups means no measured estimate",
          enrich.eta_from_measurement(db) is None)

    db.query(CachedImage).delete()
    db.query(Item).delete()
    db.commit()
    db.close()


def test_the_amiami_budget_is_shared_out_and_used() -> None:
    print("\n== One allowance, shared by what matters, never left idle ==")
    from app.config import settings
    from app.services import budget

    total = budget.total_per_minute()

    # Fixed per-job rates cannot lend. The sampler was given ten a minute for
    # four minutes in every ten - four a minute averaged, measured at 3.4 -
    # while nothing else was using the rest of the allowance.
    check("a job on its own gets the whole pool",
          budget.rate_for("shelf") == total, budget.rate_for("shelf"))

    with budget.claim("catalogue"):
        alone = budget.rate_for("catalogue")
        check("so does a sweep", alone == total, alone)
        shelf_beside = budget.rate_for("shelf")
        check("and a sampler starting up gets the rest",
              0 < shelf_beside < total, shelf_beside)

        with budget.claim("shelf"):
            sweep = budget.rate_for("catalogue")
            shelf = budget.rate_for("shelf")
            check("with both running the sweep gets more", sweep > shelf, (sweep, shelf))
            check("and between them they use the pool",
                  abs(sweep + shelf - total) < 0.01, (sweep, shelf, total))

            with budget.claim("watch"):
                watch = budget.rate_for("watch")
                check("a watch outranks both",
                      watch > budget.rate_for("catalogue") > budget.rate_for("shelf"),
                      (watch, budget.rate_for("catalogue"), budget.rate_for("shelf")))
                check(
                    "and all three still add up to the pool",
                    abs(
                        watch + budget.rate_for("catalogue") + budget.rate_for("shelf") - total
                    ) < 0.01,
                )

    check("everything releases its claim", budget.snapshot()["running"] == [])
    check("and the pool is whole again", budget.rate_for("shelf") == total)

    # What it buys the sampler, which is the job this was written for.
    duty = settings.shelf_max_seconds_per_run / 60 / settings.shelf_run_interval_minutes
    was = 10.0 * duty
    now = budget.rate_for("shelf") * duty
    check(f"the sampler goes from {was:.1f}/min to {now:.1f}/min when alone",
          now > was * 2, (was, now))

    # Nesting: two threads inside one job must not let the first one out.
    with budget.claim("shelf"):
        with budget.claim("shelf"):
            pass
        check("a nested claim does not release the outer one",
              "shelf" in budget.snapshot()["running"])
    check("only the last one does", budget.snapshot()["running"] == [])

    # Something that never declared a weight is still paced rather than
    # given the run of the place.
    stranger = budget.rate_for("something-else")
    check("an unknown job gets a modest share", 0 < stranger < total, stranger)


def test_the_pacer_follows_the_shared_rate() -> None:
    print("\n== The pacing follows the pool, and stays irregular ==")
    from app.services import budget
    from app.services.pacing import HumanPacer

    fixed = HumanPacer(requests_per_minute=10.0)
    check("a fixed pacer reports its own rate", fixed.current_rate == 10.0)
    check("and does not claim to be sharing", not fixed.stats()["rate_is_shared"])

    shared = HumanPacer(rate_source=lambda: budget.rate_for("shelf"))
    alone = shared.current_rate
    check("a shared pacer reads the pool", alone == budget.total_per_minute(), alone)
    check("and says so", shared.stats()["rate_is_shared"])

    with budget.claim("watch"), budget.claim("catalogue"):
        crowded = shared.current_rate
        check("it slows down when others start", crowded < alone, (alone, crowded))
    check("and speeds up again when they stop", shared.current_rate == alone)

    # The whole point of the pacer survives: the gaps are still drawn, not
    # spaced evenly. A steady stream would be easier to write and would look
    # exactly like a machine.
    delays = [shared.next_delay() for _ in range(60)]
    check("the gaps vary", len(set(round(d, 2) for d in delays)) > 30, len(set(delays)))
    check("none is below the floor", min(delays) >= shared.minimum_delay)
    check("and the mean lands near the target",
          abs(sum(delays) / len(delays) - shared.mean_delay) < shared.mean_delay,
          (sum(delays) / len(delays), shared.mean_delay))


def test_failures_are_attributed_and_requests_are_readable() -> None:
    print("\n== A failure says which job it belongs to ==")
    from app.providers.base import _describe
    from app.services import reqlog

    reqlog._events.clear()
    reqlog._recent.clear()
    reqlog._doing.clear()

    with reqlog.purpose("catalogue"):
        for n in range(6):
            reqlog.record("amiami", ok=(n != 2), url=f"/api/v1.0/items?pagecnt={n}",
                          status=200 if n != 2 else 503, ms=180.0)
    with reqlog.purpose("shelf"):
        for n in range(3):
            reqlog.record("amiami", url=f"/api/v1.0/item?gcode=F-{n}", status=200, ms=120.0)

    host = reqlog.rates(60)["hosts"]["amiami"]
    by_key = {p["key"]: p for p in host["purposes"]}
    check("the total is still there", host["errors"] == 1, host["errors"])
    check("and now says which job", by_key["catalogue"]["errors"] == 1)
    check("leaving the innocent one alone", by_key["shelf"]["errors"] == 0)

    # A request is written down as it went out, so a debug view can settle
    # whether a setting took effect rather than merely asserting it did.
    line = _describe("https://api.amiami.com/api/v1.0/items",
                     {"pagemax": 50, "pagecnt": 14, "s_sortkey": "preowned", "lang": "eng"})
    check("the host is dropped", not line.startswith("http"), line)
    check("the path is kept", line.startswith("/api/v1.0/items"), line)
    check("and the query is legible", "s_sortkey=preowned" in line, line)
    check("a value of None is left out",
          "lang" not in _describe("/x", {"lang": None}), _describe("/x", {"lang": None}))

    # The debug view: what it is doing, and what just went out.
    reqlog.doing("catalogue", "Pre-owned figures: page 14 of 30",
                 sort_key="preowned", page=14)
    view = reqlog.debug("catalogue")
    check("it says what it is doing", view["doing"]["what"].endswith("page 14 of 30"))
    check("with the detail the log cannot know", view["doing"]["sort_key"] == "preowned")
    check("and how long it has been at it", view["doing"]["for_seconds"] >= 0)
    check("the trail is newest first", view["recent"][0]["url"].endswith("pagecnt=5"),
          view["recent"][0]["url"])
    check("and holds the failure", any(not e["ok"] for e in view["recent"]))

    reqlog.done("catalogue")
    check("a finished job reports nothing in flight",
          reqlog.debug("catalogue")["doing"] is None)

    reqlog._events.clear()
    reqlog._recent.clear()


def test_a_deleted_listing_is_still_reachable_by_its_code() -> None:
    print("\n== Typing the code of something the shop deleted opens our copy ==")
    from app.api.search import resolve
    from app.db import SessionLocal, init_db
    from app.models import Condition, CostProfile, Item, Listing, PricePoint, User
    from app.providers import ItemNotFound
    from app.schemas import ResolveRequest
    import app.providers.amiami as amiami_provider

    init_db()
    db = SessionLocal()
    db.query(PricePoint).delete()
    db.query(Listing).delete()
    db.query(Item).delete()
    db.commit()

    kept = Item(provider="amiami", code="FIGURE-009234-R",
                name="Deleted upstream, kept here", condition=Condition.preowned,
                currency="JPY", in_stock=False, order_closed=True, current_price=4980)
    db.add(kept)
    db.commit()

    user = db.query(User).first() or User(email="probe@example.invalid",
                                          username="probe", password_hash="x")
    profile = CostProfile(user_id=user.id or 1)

    # The shop's answer for a sold-out pre-owned listing: it is simply gone.
    original = amiami_provider.AmiAmiProvider.get_item

    def deleted(self, code):  # noqa: ANN001
        raise ItemNotFound("AmiAmi no longer lists this item")

    amiami_provider.AmiAmiProvider.get_item = deleted
    try:
        # The search box treats anything shaped like a product code as a
        # request to open that product. It used to ask the shop and stop
        # there, so typing the code of a listing AmiAmi had deleted answered
        # "AmiAmi no longer lists this item" - while our own copy sat right
        # there, findable by the very same code through the ordinary search.
        out = resolve(ResolveRequest(input="FIGURE-009234-R"), db=db, user=user,
                      profile=profile)
        check("the code opens our copy", out.code == "FIGURE-009234-R", out.code)
        check("with what we recorded", out.name.startswith("Deleted upstream"))
        check("and it knows the shop dropped it", out.order_closed)

        # A full shop URL for the same product behaves the same way.
        out = resolve(
            ResolveRequest(input="https://www.amiami.com/eng/detail/?gcode=FIGURE-009234-R"),
            db=db, user=user, profile=profile,
        )
        check("a pasted link works too", out.code == "FIGURE-009234-R")

        # Something we have never seen still says so rather than inventing a
        # row: the fallback is to our catalogue, not to silence.
        raised = None
        try:
            resolve(ResolveRequest(input="FIGURE-999999-R"), db=db, user=user, profile=profile)
        except Exception as exc:  # noqa: BLE001
            raised = exc
        check("an unknown code is still a miss", getattr(raised, "status_code", None) == 404,
              raised)
    finally:
        amiami_provider.AmiAmiProvider.get_item = original

    db.query(Item).delete()
    db.commit()
    db.close()


def test_a_failing_slice_rests_rather_than_stopping_for_good() -> None:
    print("\n== A slice that failed a few times comes back on its own ==")
    from app.db import SessionLocal, init_db
    from app.models import CatalogCrawl
    from app.services import crawler
    from app.services.crawler import ERROR_PATIENCE, error_rest_seconds, resting_after_errors

    now = datetime.now(timezone.utc)

    def slice_with(failures: int, minutes_ago: float) -> CatalogCrawl:
        return CatalogCrawl(
            provider="amiami", scope="figures_preowned", pages_total=211,
            consecutive_errors=failures,
            last_error_at=now - timedelta(minutes=minutes_ago),
        )

    # The trap this replaces: five consecutive failures struck the slice off
    # the candidate list outright. A slice that is never selected can never
    # succeed, and only a success cleared the counter - so one bad patch of
    # network stopped it for good, silently, with the panel still reading
    # "running" from whenever it last did.
    check("under the limit it keeps going",
          not resting_after_errors(slice_with(ERROR_PATIENCE - 1, 1)))
    check("at the limit it rests", resting_after_errors(slice_with(ERROR_PATIENCE, 1)))
    check("but not for ever", not resting_after_errors(slice_with(ERROR_PATIENCE, 60)))

    # Each further run of failures rests longer, so a shop that is genuinely
    # unhappy is not hammered - but it is always tried again eventually.
    short = error_rest_seconds(slice_with(ERROR_PATIENCE, 0))
    longer = error_rest_seconds(slice_with(ERROR_PATIENCE + 3, 0))
    check("a worse run rests longer", longer > short, (short, longer))
    check("with a ceiling on it",
          error_rest_seconds(slice_with(40, 0)) <= 6 * 3600 + 1,
          error_rest_seconds(slice_with(40, 0)))
    check("and a slice that has never failed rests not at all",
          error_rest_seconds(CatalogCrawl(provider="a", scope="s")) == 0.0)

    # And it is actually selectable again once rested.
    init_db()
    db = SessionLocal()
    db.query(CatalogCrawl).delete()
    db.commit()
    row = CatalogCrawl(
        provider="amiami", scope="figures_preowned", enabled=True, pages_total=211,
        consecutive_errors=ERROR_PATIENCE, last_error_at=now - timedelta(minutes=1),
        cursor_page=4, cycles_completed=1, finished_at=now - timedelta(days=2),
        recheck_interval_minutes=60,
    )
    db.add(row)
    db.commit()
    check("while resting it is not picked", crawler._select_crawl(db, "amiami") is None)

    row.last_error_at = now - timedelta(hours=2)
    db.commit()
    picked = crawler._select_crawl(db, "amiami")
    check("once rested it is picked again", picked is not None and picked.scope == row.scope,
          picked.scope if picked else None)

    db.query(CatalogCrawl).delete()
    db.commit()
    db.close()


def test_spare_budget_goes_to_the_longest_unseen() -> None:
    print("\n== Spare capacity goes to whatever has waited longest ==")
    from app.db import SessionLocal, init_db
    from app.models import AppSetting, Condition, Item
    from app.services import shelfwatch

    init_db()
    db = SessionLocal()
    db.query(AppSetting).delete()
    db.query(Item).delete()
    db.commit()

    now = datetime.now(timezone.utc)
    # Nothing is due: the tiers have everything covered for the next few hours.
    for n in range(20):
        db.add(Item(provider="amiami", code=f"P-{n}", name=f"Figure {n}",
                    condition=Condition.preowned, currency="JPY", order_closed=False,
                    shelf_due_at=now + timedelta(hours=5),
                    last_detail_fetch_at=now - timedelta(days=n + 2)))
    db.commit()

    check("nothing is due", due := len(shelfwatch.due_items(db, "amiami", 50)) == 0, due)

    # The tiers would otherwise re-read whatever is hot for the twentieth time
    # before a quiet product is looked at once. With budget going spare, it is
    # worth more spent on the least recently examined.
    picked = shelfwatch.sweep_candidates(db, "amiami", 5, now - timedelta(hours=24))
    check("five candidates come back", len(picked) == 5, len(picked))
    ages = [(now - p.last_detail_fetch_at.replace(tzinfo=timezone.utc)).days for p in picked]
    check("the longest unseen come first", ages == sorted(ages, reverse=True), ages)
    check("and they are genuinely old", min(ages) > 15, ages)

    # A product looked at recently is not dragged forward.
    fresh = db.query(Item).filter_by(code="P-0").one()
    fresh.last_detail_fetch_at = now - timedelta(minutes=5)
    db.commit()
    check("something just examined is left alone",
          "P-0" not in [p.code for p in
                        shelfwatch.sweep_candidates(db, "amiami", 20,
                                                    now - timedelta(hours=24))])

    # Once a day, not a second polling loop.
    check("no sweep is recorded to start with", shelfwatch._last_sweep_at(db) is None)
    shelfwatch._note_sweep(db)
    db.commit()
    marked = shelfwatch._last_sweep_at(db)
    check("one is recorded when it runs", marked is not None)
    check("and it is recent", (now - marked).total_seconds() < 60)

    db.query(AppSetting).delete()
    db.query(Item).delete()
    db.commit()
    db.close()


def test_a_whole_pass_is_counted_before_it_is_closed() -> None:
    print("\n== A pass is counted whole, including the slot that finished it ==")
    from app.db import SessionLocal, init_db
    from app.models import CatalogCrawl
    from app.services.crawler import CrawlRun, _accumulate, _complete_cycle

    init_db()
    db = SessionLocal()
    db.query(CatalogCrawl).delete()
    db.commit()

    now = datetime.now(timezone.utc)
    crawl = CatalogCrawl(provider="amiami", scope="figures_preowned", pages_total=211,
                         head_pages=30, cursor_page=1, recent_runs=[], current_pass={},
                         full_sweep_interval_minutes=1440, last_full_sweep_at=now)
    db.add(crawl)
    db.commit()
    crawl.current_pass = {
        "started_at": (now - timedelta(minutes=10)).isoformat(),
        "pages": 0, "items": 0, "new": 0, "changed": 0, "errors": 0,
        "slots": 0, "working_seconds": 0.0, "interruptions": 0,
    }

    # Closing the pass writes it down and clears the accumulator, so the slot
    # that reached the end has to be counted first. The other way round - which
    # is how this was written - put those pages into the *next* pass, and every
    # logged pass came out short by however much it did last. A thirty-page
    # short pass was recorded as 13, 22, 26: anything but thirty.
    for pages in (12, 11, 7):
        _accumulate(crawl, CrawlRun(pages=pages, items=pages * 50, seconds=90.0))
    _complete_cycle(db, crawl)

    entry = crawl.recent_runs[0]
    check("all thirty pages are in the record", entry["pages"] == 30, entry["pages"])
    check("across all three slots", entry["slots"] == 3, entry["slots"])
    check("and the next pass starts empty", crawl.current_pass == {}, crawl.current_pass)

    db.query(CatalogCrawl).delete()
    db.commit()
    db.close()


def main() -> int:
    test_url_parsing()
    test_release_dates()
    test_landed_cost()
    test_weight_estimation()
    test_weights_match_real_parcels()
    test_shipping_table()
    test_amiami_shipping_rates()
    test_packaging_weight()
    test_shelf_sequences()
    test_kaplan_meier()
    test_shelf_reconciliation()
    test_copy_claimed_by_another_product()
    test_shelf_wholesale_disappearance()
    test_shelf_intake_estimate()
    test_shelf_tiers()
    test_wishlist_covers_both_conditions()
    test_search_can_fold_conditions()
    test_timestamps_keep_their_zone()
    test_null_lists_never_break_a_response()
    test_blocklist_hides_things_while_browsing()
    test_rail_filters_take_lists()
    test_slices_take_turns()
    test_sweep_estimate_uses_observed_speed()
    test_slice_counts_compare_like_with_like()
    test_activity_can_start_again()
    test_discover_ignores_placeholder_series()
    test_history_pruning_keeps_the_irreplaceable()
    test_deleting_a_copy_keeps_its_prices()
    test_image_prune_protects_the_unfetchable()
    test_price_survives_a_quiet_response()
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
    test_crawler_cooldown()
    test_crawler_coverage_counting()
    test_crawler_yields_to_watches()
    test_mfc_cookie_parsing()
    test_health_detection()
    test_image_url_derivation()
    test_image_selection()
    test_local_search()
    test_watch_code_normalisation()
    test_image_fallback()
    test_run_log()
    test_request_accounting()
    test_the_amiami_budget_is_shared_out_and_used()
    test_the_pacer_follows_the_shared_rate()
    test_failures_are_attributed_and_requests_are_readable()
    test_a_deleted_listing_is_still_reachable_by_its_code()
    test_photo_counts_distinguish_known_from_held()
    test_prefetch_works_through_its_backlog()
    test_the_photo_queue_is_reached_however_full_the_cache_is()
    test_waits_are_quoted_from_what_was_measured()
    test_tag_search_reaches_past_the_popular_ones()
    test_price_history_follows_the_cheapest_copy()
    test_head_slice_reads_only_its_front()
    test_copy_codes_are_not_product_codes()
    test_shop_links_use_the_right_key()
    test_a_watch_waiting_is_not_a_watch_failing()
    test_linking_estimate_describes_the_job_that_runs()
    test_daily_recap_counts_copies_not_products()
    test_condition_filter_asks_about_copies()
    test_grades_match_exactly_unless_widened()
    test_condition_notes_are_told_from_shipping_notices()
    test_shop_notes_hold_up_on_wording_never_seen()
    test_a_note_belongs_to_one_copy()
    test_each_copy_carries_its_own_price_trail()
    test_a_refresh_notices_a_copy_has_gone()
    test_a_short_pass_stops_at_its_own_edge()
    test_a_whole_pass_is_counted_before_it_is_closed()
    test_a_failing_slice_rests_rather_than_stopping_for_good()
    test_spare_budget_goes_to_the_longest_unseen()
    test_pruning_survives_a_large_catalogue()
    test_a_recovered_breaker_stops_reporting_itself_as_open()
    test_missing_exchange_rates_are_reported()
    test_newly_listed_used_is_not_newly_known()
    test_settings()

    print(f"\n{'=' * 46}\n  {PASS} passed, {FAIL} failed\n{'=' * 46}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
