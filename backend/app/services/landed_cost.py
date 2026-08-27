"""Landed-cost estimation.

The price AmiAmi shows is not the price you pay. For an EU buyer the real
number is goods + international shipping + customs duty + import VAT + the
carrier presentation fee. A watch can be set to trigger on either figure, so
this module has to produce both, and it has to explain itself: every result
carries a full breakdown that the UI renders as a tooltip.

Method (EU rules, configurable per user):
  1. goods    = shop price converted to the display currency, plus the FX
                spread a card issuer charges
  2. shipping = AmiAmi's own published rate chart, your own weight
                brackets, a flat number, or skipped. Every mode bills the
                packed parcel, so a packaging allowance is added on top of
                the estimated weight of the goods.
  3. duty     = (goods + shipping) * duty_rate, waived below the duty-free
                threshold (150 EUR in the EU)
  4. VAT      = (goods + shipping + duty) * vat_rate, charged from the first
                cent since the EU dropped its 22 EUR exemption in 2021
  5. handling = flat carrier clearance fee, only when tax is actually levied
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from sqlalchemy.orm import Session

from ..models import CostProfile, Item
from . import fx, shipping_rates

# Rough shipping weights, in grams, keyed by a token found in the product
# name. Deliberately generous: underestimating shipping is the failure mode
# that costs the user money.
DEFAULT_CATEGORY_WEIGHTS: dict[str, int] = {
    "nendoroid": 400,
    "figma": 450,
    "trading figure": 250,
    "acrylic": 150,
    "keychain": 120,
    "plush": 500,
    "1/8": 900,
    "1/7": 1200,
    "1/6": 1800,
    "1/4": 3500,
    "statue": 3000,
    "plastic model": 1300,
    "model kit": 1300,
    "figure": 900,
}

_SIZE_RE = re.compile(r"H\s*(\d{2,4})\s*mm", re.IGNORECASE)

#: Words that describe almost any product and must lose to anything specific.
#: Without this, "Honolulu 1/7 Complete Figure" would match "figure" (900 g)
#: before "1/7" (1200 g) purely because the word is longer.
_GENERIC_WEIGHT_KEYS = {"figure", "statue", "toy", "goods"}


def _specificity(key: str) -> tuple[int, int]:
    """Match order for weight keywords: scales first, generics last."""
    if "/" in key:
        return (0, -len(key))
    if key.lower() in _GENERIC_WEIGHT_KEYS:
        return (2, -len(key))
    return (1, -len(key))


@dataclass(slots=True)
class CostBreakdown:
    currency: str
    goods: float
    shipping: float
    duty: float
    vat: float
    handling: float
    total: float
    weight_grams: int
    fx_rate: float | None
    duty_rate: float
    vat_rate: float
    duty_waived: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = asdict(self)
        for key in ("goods", "shipping", "duty", "vat", "handling", "total"):
            data[key] = round(data[key], 2)
        return data


def default_profile(user_id: int) -> CostProfile:
    """A sensible German default, used until the user edits their profile."""
    return CostProfile(
        user_id=user_id,
        country="DE",
        vat_rate=0.19,
        duty_rate=0.047,
        duty_free_threshold=150.0,
        customs_handling_fee=6.0,
        shipping_mode="amiami",
        shipping_zone="zone3",
        shipping_service="auto_air",
        shipping_flat=25.0,
        shipping_table=[
            {"max_grams": 500, "cost": 14.0},
            {"max_grams": 1000, "cost": 20.0},
            {"max_grams": 2000, "cost": 30.0},
            {"max_grams": 5000, "cost": 48.0},
            {"max_grams": 10000, "cost": 78.0},
        ],
        default_weight_grams=900,
        packaging_grams=400,
        category_weights=dict(DEFAULT_CATEGORY_WEIGHTS),
        consolidate_shipping=False,
        fx_markup=0.015,
    )


def estimate_weight(item: Item | None, profile: CostProfile) -> int:
    """Best guess at shipping weight, in grams."""
    if item is None:
        return profile.default_weight_grams or 900

    weights = {**DEFAULT_CATEGORY_WEIGHTS, **(profile.category_weights or {})}

    # An explicit height in the spec sheet beats any keyword guess.
    spec = item.spec or ""
    size_match = _SIZE_RE.search(spec)
    if size_match:
        height_mm = int(size_match.group(1))
        # Boxed weight scales roughly with the cube of height. Anchored on a
        # 260 mm 1/7 scale figure shipping at about 1.2 kg.
        estimated = int(1200 * (height_mm / 260.0) ** 2.4)
        return max(150, min(estimated, 15000))

    haystack = " ".join(filter(None, [item.name, item.scale, item.category])).lower()
    for key in sorted(weights, key=_specificity):
        if key.lower() in haystack:
            return int(weights[key])
    return profile.default_weight_grams or 900


def shipment_weight(item: Item | None, profile: CostProfile, quantity: int = 1) -> int:
    """Weight the carrier actually bills: the goods plus their packaging.

    The box, the padding and the paperwork travel too, and AmiAmi's rate
    charts are read off the parcel on the scale rather than the figure inside
    it. Packaging is charged once per shipment however many units are in it.
    """
    goods = estimate_weight(item, profile) * max(1, quantity)
    packaging = profile.packaging_grams
    if packaging is None:
        packaging = 400
    return goods + max(0, int(packaging))


def _amiami_shipping(
    weight_grams: int,
    profile: CostProfile,
    db: Session | None,
    target_currency: str,
) -> tuple[float, str] | None:
    """Quote from AmiAmi's own charts, or None if they cannot answer."""
    zone = profile.shipping_zone or shipping_rates.zone_for_country(profile.country)
    if zone not in shipping_rates.RATES:
        zone = shipping_rates.zone_for_country(profile.country)

    chosen = (profile.shipping_service or "auto_air").lower()
    detail = ""
    if chosen in ("auto", "auto_air"):
        air_only = chosen == "auto_air"
        quote = shipping_rates.cheapest(zone, weight_grams, air_only=air_only)
        if quote is None:
            return None
        service, yen = quote
        detail = ", the cheapest air service" if air_only else ", the cheapest service"
        detail += " that takes the weight"
    else:
        yen = shipping_rates.lookup(zone, chosen, weight_grams)
        service = chosen
        if yen is None:
            # Small packet stops at 2 kg, so a heavy parcel has to fall
            # through to a service that will actually carry it. Stay on air
            # unless the user deliberately asked for sea mail.
            air_only = chosen not in shipping_rates.SURFACE_SERVICES
            fallback = shipping_rates.cheapest(zone, weight_grams, air_only=air_only)
            if fallback is None:
                fallback = shipping_rates.cheapest(zone, weight_grams)
            if fallback is None:
                return None
            cap = shipping_rates.heaviest(zone, chosen)
            service, yen = fallback
            label = shipping_rates.SERVICES.get(chosen, chosen)
            detail = f", switched from {label} which stops at {cap} g"

    rate = fx.get_rate(db, "JPY", target_currency.upper()) if db is not None else None
    if rate is None:
        return None

    name = shipping_rates.SERVICES.get(service, service)
    note = f"AmiAmi {name}, {weight_grams} g to {shipping_rates.ZONES[zone]}{detail}"
    return yen * rate, note


def shipping_cost(
    weight_grams: int,
    profile: CostProfile,
    db: Session | None = None,
    target_currency: str = "EUR",
) -> tuple[float, str]:
    """Shipping in the profile currency, plus a note explaining the number."""
    mode = (profile.shipping_mode or "table").lower()
    if mode == "none":
        return 0.0, "Shipping excluded by your cost profile"
    if mode == "amiami":
        quote = _amiami_shipping(weight_grams, profile, db, target_currency)
        if quote is not None:
            return quote
        # Off the end of the charts, or no yen rate to convert with. Either
        # way the weight table below is a better answer than nothing.
    elif mode == "flat":
        return float(profile.shipping_flat or 0.0), "Flat shipping rate"

    table = sorted(
        (row for row in (profile.shipping_table or []) if row.get("max_grams")),
        key=lambda row: row["max_grams"],
    )
    prefix = "Estimated: " if mode == "amiami" else ""
    if not table:
        return float(profile.shipping_flat or 0.0), f"{prefix}no weight table set, using flat rate"
    for row in table:
        if weight_grams <= row["max_grams"]:
            return float(row["cost"]), f"{prefix}weight bracket up to {row['max_grams']} g"
    top = table[-1]
    # Beyond the table, extrapolate linearly from the top bracket.
    per_gram = top["cost"] / max(1, top["max_grams"])
    return float(weight_grams * per_gram), f"{prefix}extrapolated beyond the heaviest bracket"


def estimate(
    db: Session,
    price: float | None,
    source_currency: str,
    profile: CostProfile,
    target_currency: str = "EUR",
    item: Item | None = None,
    quantity: int = 1,
) -> CostBreakdown | None:
    """Full landed cost for ``quantity`` units of ``item``.

    Returns None when the price is unknown or no exchange rate is available,
    so callers can fall back to showing the shop price untouched.
    """
    if price is None:
        return None

    rate = fx.get_rate(db, source_currency.upper(), target_currency.upper())
    if rate is None:
        return None

    notes: list[str] = []
    markup = float(profile.fx_markup or 0.0)
    goods = price * rate * (1.0 + markup) * max(1, quantity)
    if markup:
        notes.append(f"Includes a {markup * 100:.1f}% card FX spread")

    weight = shipment_weight(item, profile, quantity)
    ship, ship_note = shipping_cost(weight, profile, db, target_currency)
    packaging = max(0, int(profile.packaging_grams or 0))
    if packaging:
        notes.append(f"Weight includes a {packaging} g packaging allowance")
    if profile.consolidate_shipping:
        # The user batches orders, so charge a share rather than a full parcel.
        ship = ship / 2.0
        ship_note += ", halved for consolidated shipping"
    notes.append(ship_note)

    dutiable_base = goods + ship
    duty_rate = float(profile.duty_rate or 0.0)
    threshold = float(profile.duty_free_threshold or 0.0)
    duty_waived = bool(threshold) and goods <= threshold
    duty = 0.0 if duty_waived else dutiable_base * duty_rate
    if duty_waived:
        notes.append(f"Duty waived below {threshold:.0f} {target_currency} goods value")

    vat_rate = float(profile.vat_rate or 0.0)
    vat_base = dutiable_base + duty
    vat = 0.0
    if goods >= float(profile.vat_free_threshold or 0.0):
        vat = vat_base * vat_rate
    else:
        notes.append("Below your import VAT threshold")

    handling = float(profile.customs_handling_fee or 0.0) if (duty + vat) > 0 else 0.0
    if handling:
        notes.append("Carrier customs presentation fee applied")

    total = goods + ship + duty + vat + handling
    return CostBreakdown(
        currency=target_currency.upper(),
        goods=goods,
        shipping=ship,
        duty=duty,
        vat=vat,
        handling=handling,
        total=total,
        weight_grams=weight,
        fx_rate=rate,
        duty_rate=0.0 if duty_waived else duty_rate,
        vat_rate=vat_rate,
        duty_waived=duty_waived,
        notes=notes,
    )


def to_shop_currency(
    db: Session,
    target_total: float,
    target_currency: str,
    source_currency: str,
    profile: CostProfile,
    item: Item | None = None,
) -> float | None:
    """Invert :func:`estimate`: what shop price yields this landed total?

    Needed because a watch with a landed target is set in EUR while the shop
    quotes JPY, and it is useful to be able to show the user which shop price
    their target corresponds to.

    The forward calculation is piecewise: duty switches on once the goods
    value passes the duty-free threshold. The inverse has to respect the same
    break, so it solves the no-duty branch first and only falls through to the
    dutiable branch when the answer lands above the threshold.
    """
    rate = fx.get_rate(db, source_currency.upper(), target_currency.upper())
    if not rate:
        return None

    weight = shipment_weight(item, profile)
    ship, _ = shipping_cost(weight, profile, db, target_currency)
    if profile.consolidate_shipping:
        ship /= 2.0

    vat_rate = float(profile.vat_rate or 0.0)
    duty_rate = float(profile.duty_rate or 0.0)
    threshold = float(profile.duty_free_threshold or 0.0)
    markup = float(profile.fx_markup or 0.0)
    # The handling fee is only charged when tax is actually levied.
    handling = float(profile.customs_handling_fee or 0.0) if (vat_rate or duty_rate) else 0.0

    def to_source(goods: float) -> float:
        return max(0.0, goods) / (rate * (1.0 + markup))

    # Branch 1: below the duty-free threshold, so total = (g + s)(1 + vat) + h
    goods = ((target_total - handling) / (1.0 + vat_rate)) - ship
    if goods <= threshold:
        return to_source(goods)

    # Branch 2: dutiable, so total = (g + s)(1 + duty)(1 + vat) + h
    goods = ((target_total - handling) / ((1.0 + duty_rate) * (1.0 + vat_rate))) - ship
    if goods >= threshold:
        return to_source(goods)

    # The target falls in the gap the duty step creates. The threshold itself
    # is the largest goods value that still satisfies it.
    return to_source(threshold)
