"""SQLAlchemy ORM models.

Design notes
------------
* Everything shop-specific hangs off ``provider`` (a short string id such as
  ``amiami``) so a second marketplace can be added without a schema change.
* ``Item`` is the canonical, deduplicated product row. ``PricePoint`` rows are
  only written when something actually changed, which keeps the history table
  small and makes the step chart in the UI honest.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """A timestamp that still knows it is UTC when it comes back out.

    SQLite has no timezone type, so ``UTCDateTime`` accepts an
    aware value on the way in and hands back a naive one on the way out.
    Everything downstream then has to remember to re-attach UTC, and the place
    that forgot was the API itself: it emitted timestamps with no offset
    suffix, and a browser reads those as local time. Every date in the
    interface was therefore wrong by the viewer's own offset - which is why a
    slice that had just run reported having run two hours ago, for ever.

    Fixing it at the column means nothing above has to think about it.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class WatchKind(str, enum.Enum):
    search = "search"
    item = "item"


class PriceBasis(str, enum.Enum):
    listed = "listed"
    landed = "landed"


class Condition(str, enum.Enum):
    any = "any"
    new = "new"
    preowned = "preowned"


class StockFilter(str, enum.Enum):
    any = "any"
    in_stock = "in_stock"
    preorder = "preorder"
    backorder = "backorder"


class TriggerType(str, enum.Enum):
    price_below = "price_below"
    back_in_stock = "back_in_stock"
    new_match = "new_match"
    price_drop = "price_drop"
    restock_preowned = "restock_preowned"
    deal_radar = "deal_radar"


class ChannelType(str, enum.Enum):
    telegram = "telegram"
    webpush = "webpush"
    email = "email"
    discord = "discord"
    ntfy = "ntfy"
    gotify = "gotify"
    webhook = "webhook"


class ListingStatus(str, enum.Enum):
    """Whether an individual copy is still on the shelf."""

    live = "live"
    gone = "gone"


class ListingOutcome(str, enum.Enum):
    """Why a copy left the shelf, as far as we can tell from outside."""

    #: It vanished on its own while the product stayed listed. On AmiAmi a
    #: sold pre-owned copy is deleted rather than flagged, so this is by far
    #: the most likely reading, but a withdrawal looks identical.
    sold = "sold"
    #: The whole product went away, taking every copy with it.
    delisted = "delisted"
    #: It went away in a batch with its siblings, which smells like a shop
    #: side action rather than several simultaneous sales.
    withdrawn = "withdrawn"
    unknown = "unknown"


class CollectionStatus(str, enum.Enum):
    wishlist = "wishlist"
    ordered = "ordered"
    owned = "owned"
    sold = "sold"


class DeliveryStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
    skipped = "skipped"


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.user)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Presentation and landed-cost preferences live on the user so a shared
    # instance can serve people in different countries.
    display_currency: Mapped[str] = mapped_column(String(3), default="EUR")
    theme: Mapped[str] = mapped_column(String(32), default="midnight")
    color_mode: Mapped[str] = mapped_column(String(16), default="dark")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Berlin")
    prefs: Mapped[dict] = mapped_column(JSON, default=dict)

    watches: Mapped[list["Watch"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    channels: Mapped[list["NotificationChannel"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    collection: Mapped[list["CollectionEntry"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    cost_profile: Mapped["CostProfile"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )


class CostProfile(Base):
    """Per-user shipping and customs assumptions for landed-cost estimates."""

    __tablename__ = "cost_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )

    #: Things you never want to see. Words are matched anywhere in a product
    #: name; tags are MyFigureCollection slugs. Applied to browsing - search
    #: results and the discovery feed - and never to a watch, because a watch
    #: is something you asked for by name and hiding its results would be a
    #: silent failure rather than a tidy list.
    blocked_terms: Mapped[list] = mapped_column(JSON, default=list)
    blocked_tags: Mapped[list] = mapped_column(JSON, default=list)

    country: Mapped[str] = mapped_column(String(2), default="DE")
    vat_rate: Mapped[float] = mapped_column(Float, default=0.19)
    # Toys and figures (TARIC 9503) land around 4.7 percent into the EU.
    duty_rate: Mapped[float] = mapped_column(Float, default=0.047)
    # Below this goods value the EU waives customs duty (VAT still applies).
    duty_free_threshold: Mapped[float] = mapped_column(Float, default=150.0)
    vat_free_threshold: Mapped[float] = mapped_column(Float, default=0.0)
    # Flat carrier presentation fee, DHL charges about 6 EUR.
    customs_handling_fee: Mapped[float] = mapped_column(Float, default=6.0)

    # amiami = the shop's own published rate charts, table = your own weight
    # brackets, flat = one number, none = skip shipping entirely
    shipping_mode: Mapped[str] = mapped_column(String(32), default="amiami")
    # Which column of AmiAmi's charts you are quoted, and which service to
    # price. The two auto modes take the cheapest service that will carry the
    # weight, which matters because small packet stops at 2 kg; auto_air
    # leaves out surface mail, which is cheap but takes months.
    shipping_zone: Mapped[str] = mapped_column(String(16), default="zone3")
    shipping_service: Mapped[str] = mapped_column(String(32), default="auto_air")
    shipping_flat: Mapped[float] = mapped_column(Float, default=25.0)
    shipping_table: Mapped[list] = mapped_column(JSON, default=list)
    default_weight_grams: Mapped[int] = mapped_column(Integer, default=800)
    # Box, padding and packing slip, added on top of every shipment because
    # the carrier bills the parcel, not the figure inside it.
    packaging_grams: Mapped[int] = mapped_column(Integer, default=250)
    # Multiplies every estimated goods weight. Real parcels for figures of much
    # the same size have come in anywhere between 1.0 and 1.5 kg, so no single
    # table gets everyone right; this is the one dial that shifts the whole
    # estimate without editing the table row by row.
    weight_scale: Mapped[float] = mapped_column(Float, default=1.0)
    category_weights: Mapped[dict] = mapped_column(JSON, default=dict)
    consolidate_shipping: Mapped[bool] = mapped_column(Boolean, default=False)
    # Payment provider FX spread, added on top of the mid-market rate.
    fx_markup: Mapped[float] = mapped_column(Float, default=0.015)

    user: Mapped[User] = relationship(back_populates="cost_profile")


class AuthSession(Base):
    """Issued tokens, kept so sessions can be revoked from the UI."""

    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


class Item(Base):
    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("provider", "code", name="uq_item_provider_code"),
        Index("ix_item_provider_updated", "provider", "last_seen_at"),
        # Catalogue search sorts by these, and by the time the crawler has
        # finished there are tens of thousands of rows to sort.
        Index("ix_item_first_seen", "first_seen_at"),
        Index("ix_item_price", "current_price"),
        Index("ix_item_availability", "order_closed", "in_stock"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True, default="amiami")
    # Shop product code, for example FIGURE-153570-R
    code: Mapped[str] = mapped_column(String(64), index=True)
    #: The same figure sold new and pre-owned is two listings under two codes
    #: that differ only by an -R suffix. This is the shared half, so the two
    #: can be grouped without a join or a stored relationship.
    figure_code: Mapped[str | None] = mapped_column(String(64), index=True)

    name: Mapped[str] = mapped_column(Text)
    name_jp: Mapped[str | None] = mapped_column(Text)
    maker: Mapped[str | None] = mapped_column(String(255), index=True)
    series: Mapped[str | None] = mapped_column(String(255), index=True)
    character: Mapped[str | None] = mapped_column(String(255), index=True)
    category: Mapped[str | None] = mapped_column(String(128))
    scale: Mapped[str | None] = mapped_column(String(32))
    jan_code: Mapped[str | None] = mapped_column(String(32), index=True)

    image_url: Mapped[str | None] = mapped_column(Text)
    images: Mapped[list] = mapped_column(JSON, default=list)
    product_url: Mapped[str | None] = mapped_column(Text)

    currency: Mapped[str] = mapped_column(String(3), default="JPY")
    # MSRP including Japanese tax, used to compute the discount badge.
    list_price: Mapped[float | None] = mapped_column(Float)
    # The cheapest currently buyable listing. Pre-owned products are sold as
    # several graded copies at different prices under one product code, so
    # this is a range, and the cheapest one is what a target price must be
    # compared against.
    current_price: Mapped[float | None] = mapped_column(Float)
    price_max: Mapped[float | None] = mapped_column(Float)
    # [{"code": "FIGURE-165063-R152", "price": 53980, "condition": "Item:B+ Box:B"}]
    variants: Mapped[list] = mapped_column(JSON, default=list)
    lowest_price: Mapped[float | None] = mapped_column(Float)
    highest_price: Mapped[float | None] = mapped_column(Float)
    average_price: Mapped[float | None] = mapped_column(Float)

    condition: Mapped[Condition] = mapped_column(Enum(Condition), default=Condition.new)
    # AmiAmi pre-owned grading, for example ITEM:B/BOX:B
    condition_grade: Mapped[str | None] = mapped_column(String(32))
    in_stock: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    is_preorder: Mapped[bool] = mapped_column(Boolean, default=False)
    is_backorder: Mapped[bool] = mapped_column(Boolean, default=False)
    order_closed: Mapped[bool] = mapped_column(Boolean, default=False)
    sale_status: Mapped[str | None] = mapped_column(String(64))
    release_date: Mapped[str | None] = mapped_column(String(32))
    release_date_parsed: Mapped[datetime | None] = mapped_column(UTCDateTime)

    spec: Mapped[str | None] = mapped_column(Text)
    remarks: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_detail_fetch_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # The fetch before that one. Without it a copy that appeared between two
    # polls has no upper bound on how long it had been listed, and the whole
    # shelf-life figure becomes a lower bound with no ceiling.
    prev_detail_fetch_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    detail_loaded: Mapped[bool] = mapped_column(Boolean, default=False)

    # -- Shelf life -----------------------------------------------------
    # AmiAmi numbers pre-owned copies per product: FIGURE-140238-R459 is the
    # 459th used copy it has taken in. Watching that counter move gives the
    # intake rate without following a single copy, and intake rate plus shelf
    # depth is all Little's Law needs to estimate how long a copy sits.
    intake_first_seq: Mapped[int | None] = mapped_column(Integer)
    intake_first_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    intake_last_seq: Mapped[int | None] = mapped_column(Integer)
    intake_last_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    #: Copies on the shelf at the last detail fetch, denormalized for sorting.
    listing_count: Mapped[int] = mapped_column(Integer, default=0)
    #: Shelf depth smoothed across fetches. Little's Law wants the average
    #: depth over the window, not the depth at the instant we happened to
    #: look, and one snapshot of a four-copy shelf that is usually six copies
    #: deep skews the answer badly.
    listing_count_avg: Mapped[float | None] = mapped_column(Float)
    #: Typical days a copy stays listed, cached so search can sort and badge.
    dwell_days: Mapped[float | None] = mapped_column(Float, index=True)
    #: observed = measured from copies we watched sell; intake = derived from
    #: the counter; product = the coarse whole-product figure from phase zero.
    dwell_basis: Mapped[str | None] = mapped_column(String(16))
    #: Copies we watched leave the shelf. Backs the observed figure, and
    #: elsewhere tells the reader how much evidence there is either way.
    dwell_samples: Mapped[int] = mapped_column(Integer, default=0)
    #: Days the whole product stayed listed before it sold out entirely.
    sold_out_days: Mapped[float | None] = mapped_column(Float)
    #: When this product is next due a detail fetch for shelf tracking. One
    #: column drives the whole sampler: a price range that moved on a cheap
    #: list sweep simply sets it to now, jumping the product to the front.
    shelf_due_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    #: hot | warm | cold, cached from the last scheduling decision so the
    #: admin panel can show where the request budget is going.
    shelf_tier: Mapped[str | None] = mapped_column(String(8))
    #: Last time a catalogue list sweep still found this product on sale. The
    #: head pages are re-read every half hour, so for the newest listings this
    #: is a far tighter signal than the detail fetches and costs nothing.
    last_listed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # MyFigureCollection cross-reference. The JAN barcode gives an exact match;
    # a title search is the fallback and is flagged as such so the UI can say
    # so rather than pretending it is authoritative.
    mfc_id: Mapped[int | None] = mapped_column(Integer, index=True)
    mfc_matched_by: Mapped[str | None] = mapped_column(String(16))
    mfc_url: Mapped[str | None] = mapped_column(Text)
    mfc_confidence: Mapped[float | None] = mapped_column(Float)
    mfc_fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    mfc_attempts: Mapped[int] = mapped_column(Integer, default=0)
    # The entry was identified, but MyFigureCollection withholds the page from
    # signed-out visitors, so no tags could be imported for it.
    mfc_restricted: Mapped[bool] = mapped_column(Boolean, default=False)

    listings: Mapped[list["Listing"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", passive_deletes=True
    )

    prices: Mapped[list["PricePoint"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", passive_deletes=True
    )
    tags: Mapped[list["Tag"]] = relationship(
        secondary="item_tags", back_populates="items", lazy="selectin"
    )


class PricePoint(Base):
    """One row per observed change, not per poll."""

    __tablename__ = "price_points"
    __table_args__ = (Index("ix_price_item_ts", "item_id", "recorded_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    #: Set when the point belongs to one individual copy rather than to the
    #: product as a whole, so both histories share one table. Deliberately
    #: SET NULL rather than CASCADE: the observation is part of the item's
    #: price history and has to outlive the copy row it came from.
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("listings.id", ondelete="SET NULL"), index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="JPY")
    in_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    sale_status: Mapped[str | None] = mapped_column(String(64))
    condition_grade: Mapped[str | None] = mapped_column(String(32))

    item: Mapped[Item] = relationship(back_populates="prices")
    listing: Mapped["Listing | None"] = relationship(back_populates="prices")


# ---------------------------------------------------------------------------
# Watches
# ---------------------------------------------------------------------------


class Listing(Base):
    """One individual copy offered under a product code.

    AmiAmi sells a used figure as several separately graded copies, each with
    its own code and price: FIGURE-140238-R459 is one copy of the product
    FIGURE-140238-R. Until now those lived only in Item.variants, a JSON blob
    overwritten wholesale on every detail fetch, so a copy that sold left no
    trace at all. One row per copy fixes that, and turns "how long was it
    listed" into a question the database can answer.

    Because we only see a copy when we happen to look, its lifetime is an
    interval rather than a number. The four timestamps bracket it:

        appeared_after   last look that did NOT have it   (may be NULL)
        first_seen_at    first look that DID
        last_seen_at     last look that DID
        vanished_before  first look that did NOT again    (NULL while live)

    Certain lifetime is last_seen_at - first_seen_at; the most it could have
    been is vanished_before - appeared_after. With appeared_after unknown the
    upper bound is open and the UI must say "at least", never a bare figure.
    """

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("provider", "code", name="uq_listing_provider_code"),
        Index("ix_listing_item_first_seen", "item_id", "first_seen_at"),
        # The sweep that closes out vanished copies filters on these two.
        Index("ix_listing_status_seen", "status", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        ForeignKey("items.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), default="amiami")
    #: The copy's own code, for example FIGURE-140238-R459.
    code: Mapped[str] = mapped_column(String(64), index=True)
    #: 459, parsed from the code. AmiAmi appears to number copies per product
    #: in the order it takes them in, which makes this a cumulative intake
    #: count and the basis for the rate estimate.
    sequence: Mapped[int | None] = mapped_column(Integer)

    #: Price when first seen, and the latest one. Pre-owned stock gets marked
    #: down while it sits, so the two differing is itself information.
    price: Mapped[float | None] = mapped_column(Float)
    last_price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="JPY")

    condition: Mapped[str | None] = mapped_column(String(64))
    item_grade: Mapped[str | None] = mapped_column(String(8))
    box_grade: Mapped[str | None] = mapped_column(String(8))

    appeared_after: Mapped[datetime | None] = mapped_column(UTCDateTime)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    vanished_before: Mapped[datetime | None] = mapped_column(UTCDateTime)

    status: Mapped[ListingStatus] = mapped_column(
        Enum(ListingStatus), default=ListingStatus.live, index=True
    )
    outcome: Mapped[ListingOutcome | None] = mapped_column(Enum(ListingOutcome))
    #: How many times we actually saw it, so thin data can be shown as thin.
    observations: Mapped[int] = mapped_column(Integer, default=1)

    item: Mapped[Item] = relationship(back_populates="listings")
    # No delete-orphan here on purpose. These rows belong to the item's price
    # history first and to the copy second, so losing the copy must not lose
    # the prices we recorded through it.
    prices: Mapped[list["PricePoint"]] = relationship(
        back_populates="listing", passive_deletes=True
    )


class Watch(Base):
    __tablename__ = "watches"
    __table_args__ = (Index("ix_watch_due", "enabled", "next_run_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="amiami")
    kind: Mapped[WatchKind] = mapped_column(Enum(WatchKind), default=WatchKind.search)

    label: Mapped[str] = mapped_column(String(200), default="")
    query: Mapped[str] = mapped_column(Text, default="")
    item_code: Mapped[str | None] = mapped_column(String(64), index=True)
    # Provider-agnostic filter bag: category, maker_id, series_id, character_id,
    # exclude_keywords, min_price, release_from, release_to, ...
    filters: Mapped[dict] = mapped_column(JSON, default=dict)

    condition: Mapped[Condition] = mapped_column(Enum(Condition), default=Condition.any)
    stock_filter: Mapped[StockFilter] = mapped_column(Enum(StockFilter), default=StockFilter.any)
    # Minimum acceptable pre-owned grade, e.g. "A" or "B+". A product code can
    # cover several graded copies at different prices, so these decide which
    # of them the target price is actually compared against.
    min_item_grade: Mapped[str | None] = mapped_column(String(4))
    min_box_grade: Mapped[str | None] = mapped_column(String(4))

    # Trigger configuration
    target_price: Mapped[float | None] = mapped_column(Float)
    price_basis: Mapped[PriceBasis] = mapped_column(Enum(PriceBasis), default=PriceBasis.listed)
    target_currency: Mapped[str] = mapped_column(String(3), default="JPY")
    notify_on_price_below: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_restock: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_on_new_match: Mapped[bool] = mapped_column(Boolean, default=True)
    # Fire when the price falls by at least N percent versus the last seen price.
    notify_on_price_drop_pct: Mapped[float | None] = mapped_column(Float)

    # Scheduling. interval_seconds = None means follow the adaptive scheduler.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_seconds: Mapped[int | None] = mapped_column(Integer)
    adaptive: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    # {"enabled": true, "start": "23:00", "end": "07:00", "urgent_override": true}
    quiet_hours: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per item and trigger debounce so one listing cannot spam you.
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=1800)
    max_alerts_per_day: Mapped[int] = mapped_column(Integer, default=50)

    # Empty list means: use every enabled channel the user owns.
    channel_ids: Mapped[list] = mapped_column(JSON, default=list)

    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    run_count: Mapped[int] = mapped_column(Integer, default=0)
    alert_count: Mapped[int] = mapped_column(Integer, default=0)
    last_result_hash: Mapped[str | None] = mapped_column(String(64))
    # The first poll only records what already exists. Without this, creating a
    # watch for a broad query would immediately fire one alert per result.
    baselined: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="watches")
    alerts: Mapped[list["Alert"]] = relationship(
        back_populates="watch", cascade="all, delete-orphan", passive_deletes=True
    )
    seen: Mapped[list["WatchSeenItem"]] = relationship(
        back_populates="watch", cascade="all, delete-orphan", passive_deletes=True
    )


class WatchSeenItem(Base):
    """Per-watch memory of what was already matched, and at which price."""

    __tablename__ = "watch_seen_items"
    __table_args__ = (
        UniqueConstraint("watch_id", "item_id", name="uq_watch_seen"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_price: Mapped[float | None] = mapped_column(Float)
    # The same observation expressed in the watch's comparison basis, which
    # may be a different currency and may include import costs. Target-price
    # crossings must be judged against this, never against last_price.
    last_compare_price: Mapped[float | None] = mapped_column(Float)
    last_in_stock: Mapped[bool] = mapped_column(Boolean, default=False)
    last_alert_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_trigger: Mapped[str | None] = mapped_column(String(32))

    watch: Mapped[Watch] = relationship(back_populates="seen")


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alert_user_ts", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    watch_id: Mapped[int | None] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="SET NULL"))

    trigger: Mapped[TriggerType] = mapped_column(Enum(TriggerType))
    #: Every reason this alert qualified, not only the one that named it.
    #: One item can be a new listing, under your target and back in stock all
    #: at once; sending three notifications would be spam, but throwing two of
    #: the reasons away meant filtering by them found nothing.
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(3), default="JPY")
    landed_price: Mapped[float | None] = mapped_column(Float)
    landed_currency: Mapped[str] = mapped_column(String(3), default="EUR")
    previous_price: Mapped[float | None] = mapped_column(Float)
    url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, index=True
    )

    watch: Mapped[Watch | None] = relationship(back_populates="alerts")
    item: Mapped[Item | None] = relationship()
    deliveries: Mapped[list["AlertDelivery"]] = relationship(
        back_populates="alert", cascade="all, delete-orphan", passive_deletes=True
    )


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"), index=True)
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("notification_channels.id", ondelete="SET NULL")
    )
    channel_type: Mapped[ChannelType] = mapped_column(Enum(ChannelType))
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus), default=DeliveryStatus.pending
    )
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    alert: Mapped[Alert] = relationship(back_populates="deliveries")


# ---------------------------------------------------------------------------
# Notification channels
# ---------------------------------------------------------------------------


class NotificationChannel(Base):
    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[ChannelType] = mapped_column(Enum(ChannelType))
    name: Mapped[str] = mapped_column(String(120), default="")
    # Secrets live here: bot_token, chat_id, webhook url, push subscription, ...
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=True)
    send_digest: Mapped[bool] = mapped_column(Boolean, default=False)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="channels")


# ---------------------------------------------------------------------------
# Wishlist and collection
# ---------------------------------------------------------------------------


class CollectionEntry(Base):
    __tablename__ = "collection_entries"
    __table_args__ = (UniqueConstraint("user_id", "item_id", name="uq_collection_user_item"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    status: Mapped[CollectionStatus] = mapped_column(
        Enum(CollectionStatus), default=CollectionStatus.wishlist
    )
    # 1 = grail, 2 = normal, 3 = maybe
    priority: Mapped[int] = mapped_column(Integer, default=2)
    notes: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    paid_price: Mapped[float | None] = mapped_column(Float)
    paid_currency: Mapped[str] = mapped_column(String(3), default="JPY")
    purchased_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    mfc_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="collection")
    item: Mapped[Item] = relationship()


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------


class FxRate(Base):
    __tablename__ = "fx_rates"
    __table_args__ = (UniqueConstraint("base", "quote", name="uq_fx_pair"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    base: Mapped[str] = mapped_column(String(3))
    quote: Mapped[str] = mapped_column(String(3))
    rate: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(64), default="")
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


class ProviderStat(Base):
    """Rolling health counters shown on the status page."""

    __tablename__ = "provider_stats"
    __table_args__ = (UniqueConstraint("provider", "day", name="uq_provider_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    rate_limited: Mapped[int] = mapped_column(Integer, default=0)
    avg_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utcnow, onupdate=utcnow
    )


# ---------------------------------------------------------------------------
# MyFigureCollection tag index
# ---------------------------------------------------------------------------


class TagKind(str, enum.Enum):
    tag = "tag"
    origin = "origin"
    character = "character"
    company = "company"
    artist = "artist"
    material = "material"
    classification = "classification"


class Tag(Base):
    """A MyFigureCollection tag or entry, mirrored locally.

    Keeping our own copy is what makes the discovery page fast: once an item
    has been enriched, filtering by tag is a local join instead of a scrape.
    """

    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("kind", "slug", name="uq_tag_kind_slug"),
        Index("ix_tag_usage", "kind", "usage_count"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[TagKind] = mapped_column(Enum(TagKind), default=TagKind.tag, index=True)
    slug: Mapped[str] = mapped_column(String(160), index=True)
    name: Mapped[str] = mapped_column(String(200))
    mfc_id: Mapped[int | None] = mapped_column(Integer, index=True)
    #: How many local items carry this tag. Drives ranking in the tag picker.
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    is_auto: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    items: Mapped[list["Item"]] = relationship(secondary="item_tags", back_populates="tags")


class ItemTag(Base):
    __tablename__ = "item_tags"
    __table_args__ = (UniqueConstraint("item_id", "tag_id", name="uq_item_tag"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tags.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DiscoverySeed(Base):
    """Cache of MFC tag browse results.

    A tag page on MFC is a scrape, so results are cached and reused. Each row
    is one MFC listing we saw for a tag combination, with the AmiAmi lookup
    result attached once we have tried to find it.
    """

    __tablename__ = "discovery_seeds"
    __table_args__ = (
        UniqueConstraint("tag_key", "mfc_id", name="uq_seed_tag_item"),
        Index("ix_seed_tagkey", "tag_key", "rank"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tag_key: Mapped[str] = mapped_column(String(255), index=True)
    mfc_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    image_url: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(64))
    rank: Mapped[int] = mapped_column(Integer, default=0)
    matched_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("items.id", ondelete="SET NULL")
    )
    lookup_state: Mapped[str] = mapped_column(String(16), default="pending")
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Catalogue ingest
# ---------------------------------------------------------------------------


class CrawlState(str, enum.Enum):
    idle = "idle"
    running = "running"
    paused = "paused"
    completed = "completed"
    failed = "failed"


class CatalogCrawl(Base):
    """A resumable sweep of a shop's catalogue.

    Discovery is only as good as the corpus behind it, and a corpus made only
    of things the user happened to search for is not a corpus. This walks the
    shop page by page in the background and records every item and its price,
    so the local catalogue reflects the shop rather than the search history.

    Each row keeps its own cursor, so a restart resumes where it left off
    instead of starting the sweep again.
    """

    __tablename__ = "catalog_crawls"
    __table_args__ = (UniqueConstraint("provider", "scope", name="uq_crawl_provider_scope"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="amiami", index=True)
    #: Which slice of the shop this sweep covers, e.g. figures_preowned.
    scope: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(120), default="")
    #: Provider-specific query for the slice, stored so scopes stay data.
    query: Mapped[dict] = mapped_column(JSON, default=dict)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Lower runs first, so the interesting slices fill in before the long tail.
    priority: Mapped[int] = mapped_column(Integer, default=100)
    state: Mapped[CrawlState] = mapped_column(Enum(CrawlState), default=CrawlState.idle)

    #: Next page to fetch. 1-based, matching the upstream API.
    cursor_page: Mapped[int] = mapped_column(Integer, default=1)
    per_page: Mapped[int] = mapped_column(Integer, default=50)
    total_results: Mapped[int] = mapped_column(Integer, default=0)
    pages_total: Mapped[int] = mapped_column(Integer, default=0)

    #: After a first full sweep, later cycles only re-read the newest pages,
    #: because the shop lists newest first and the tail rarely changes.
    head_pages: Mapped[int] = mapped_column(Integer, default=20)
    #: How long to wait before re-reading the newest pages. Without a pause the
    #: crawler simply loops over the same first pages continuously, which is
    #: how one slice accumulated 43 passes in an afternoon.
    recheck_interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    full_sweep_interval_days: Mapped[int] = mapped_column(Integer, default=7)
    last_full_sweep_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cycles_completed: Mapped[int] = mapped_column(Integer, default=0)

    pages_fetched: Mapped[int] = mapped_column(Integer, default=0)
    #: Pages this slice actually gets through in an hour of wall-clock time,
    #: smoothed across runs. Measured rather than calculated, because the
    #: calculation was wildly optimistic: it assumed the slice had the crawler
    #: to itself and that nothing ever paused, while in reality four slices
    #: share the time, the pacer scatters its requests and takes breaks, the
    #: night slows everything down and a due watch interrupts outright.
    pages_per_hour: Mapped[float | None] = mapped_column(Float)
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_changed: Mapped[int] = mapped_column(Integer, default=0)

    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Image cache
# ---------------------------------------------------------------------------


class CachedImage(Base):
    """A product photo kept on disk.

    AmiAmi deletes a pre-owned listing the moment it sells, and its images go
    with it. Everything else about the item survives here, so without a local
    copy the record of a figure that sold is a row with a broken picture, at
    exactly the moment the history becomes worth keeping.

    Files are content-addressed by a hash of the source URL, so the same photo
    is never stored twice and the path can be derived without a lookup.
    """

    __tablename__ = "cached_images"
    __table_args__ = (Index("ix_image_lru", "last_used_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Hash of the source URL. Also the filename and the public route.
    key: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    source_url: Mapped[str] = mapped_column(Text)
    #: main or thumb. Thumbnails are tiny and cached for everything; full
    #: images are much larger and fetched on demand.
    kind: Mapped[str] = mapped_column(String(16), default="thumb", index=True)

    content_type: Mapped[str] = mapped_column(String(64), default="image/jpeg")
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)

    fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_used_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    use_count: Mapped[int] = mapped_column(Integer, default=0)

    #: Set when the origin no longer serves it, so we stop retrying and the UI
    #: can say the picture is gone rather than showing a broken frame.
    gone: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
