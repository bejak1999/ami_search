"""Pydantic request and response models."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from .models import (
    ChannelType,
    CollectionStatus,
    Condition,
    PriceBasis,
    StockFilter,
    TriggerType,
    UserRole,
    WatchKind,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: EmailStr
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=10, max_length=200)


class LoginRequest(BaseModel):
    identifier: str = Field(description="Username or e-mail address")
    password: str
    remember: bool = True


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=200)


class UserOut(ORMModel):
    id: int
    email: EmailStr
    username: str
    role: UserRole
    is_active: bool
    display_currency: str
    theme: str
    color_mode: str
    timezone: str
    prefs: dict = {}
    created_at: datetime
    last_login_at: datetime | None = None


class UserUpdate(BaseModel):
    display_currency: str | None = Field(default=None, min_length=3, max_length=3)
    theme: Literal["midnight", "sakura"] | None = None
    color_mode: Literal["dark", "light", "system"] | None = None
    timezone: str | None = None
    prefs: dict | None = None


class AuthResponse(BaseModel):
    user: UserOut
    token: str
    expires_at: datetime


# ---------------------------------------------------------------------------
# Cost profile
# ---------------------------------------------------------------------------


class ShippingBracket(BaseModel):
    max_grams: int = Field(gt=0)
    cost: float = Field(ge=0)


class CostProfileOut(ORMModel):
    country: str
    vat_rate: float
    duty_rate: float
    duty_free_threshold: float
    vat_free_threshold: float
    customs_handling_fee: float
    shipping_mode: str
    shipping_flat: float
    shipping_table: list[ShippingBracket] = []
    default_weight_grams: int
    category_weights: dict = {}
    consolidate_shipping: bool
    fx_markup: float


class CostProfileUpdate(BaseModel):
    country: str | None = Field(default=None, min_length=2, max_length=2)
    vat_rate: float | None = Field(default=None, ge=0, le=1)
    duty_rate: float | None = Field(default=None, ge=0, le=1)
    duty_free_threshold: float | None = Field(default=None, ge=0)
    vat_free_threshold: float | None = Field(default=None, ge=0)
    customs_handling_fee: float | None = Field(default=None, ge=0)
    shipping_mode: Literal["table", "flat", "none"] | None = None
    shipping_flat: float | None = Field(default=None, ge=0)
    shipping_table: list[ShippingBracket] | None = None
    default_weight_grams: int | None = Field(default=None, gt=0)
    category_weights: dict[str, int] | None = None
    consolidate_shipping: bool | None = None
    fx_markup: float | None = Field(default=None, ge=0, le=0.2)


class CostBreakdownOut(BaseModel):
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
    notes: list[str] = []


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


class ItemVariant(BaseModel):
    """One graded sub-listing under a product code."""

    code: str
    price: float
    condition: str = ""
    item_grade: str | None = None
    box_grade: str | None = None


class ItemBase(BaseModel):
    provider: str
    code: str
    name: str
    url: str | None = None
    currency: str = "JPY"
    price: float | None = None
    price_max: float | None = None
    variants: list[ItemVariant] = []
    list_price: float | None = None
    image_url: str | None = None
    maker: str | None = None
    series: str | None = None
    character: str | None = None
    scale: str | None = None
    condition: str = "new"
    condition_grade: str | None = None
    in_stock: bool = False
    is_preorder: bool = False
    is_backorder: bool = False
    order_closed: bool = False
    release_date: str | None = None
    discount_pct: float | None = None
    landed: CostBreakdownOut | None = None
    display_price: float | None = None
    display_currency: str | None = None


class ItemOut(ItemBase):
    id: int | None = None
    name_jp: str | None = None
    images: list[str] = []
    jan_code: str | None = None
    spec: str | None = None
    remarks: str | None = None
    lowest_price: float | None = None
    highest_price: float | None = None
    average_price: float | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    tracked: bool = False
    watch_count: int = 0
    in_collection: str | None = None
    mfc_id: int | None = None
    mfc_url: str | None = None
    mfc_matched_by: str | None = None
    mfc_confidence: float | None = None
    tags: list[dict] = []


class PricePointOut(ORMModel):
    recorded_at: datetime
    price: float | None
    currency: str
    in_stock: bool
    sale_status: str | None = None
    condition_grade: str | None = None


class ItemHistoryOut(BaseModel):
    item: ItemOut
    points: list[PricePointOut]
    stats: dict[str, Any]


class SearchRequest(BaseModel):
    q: str = ""
    provider: str = "amiami"
    page: int = Field(default=1, ge=1, le=200)
    per_page: int = Field(default=30, ge=1, le=50)
    condition: Literal["any", "new", "preowned"] = "any"
    stock: Literal["any", "in_stock", "preorder", "backorder"] = "any"
    sort: Literal[
        "newest", "preowned", "price_asc", "price_desc", "release", "discount"
    ] = "newest"
    category_id: int | None = None
    maker_id: int | None = None
    series_id: int | None = None
    min_price: float | None = None
    max_price: float | None = None
    exclude: list[str] = []
    on_sale: bool = False

    @field_validator("exclude", mode="before")
    @classmethod
    def _split(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value


class SearchResponse(BaseModel):
    items: list[ItemOut]
    total: int
    page: int
    per_page: int
    pages: int
    facets: dict[str, Any] = {}
    took_ms: int = 0
    provider: str


# ---------------------------------------------------------------------------
# Watches
# ---------------------------------------------------------------------------


class QuietHours(BaseModel):
    enabled: bool = False
    start: str = "23:00"
    end: str = "07:00"
    urgent_override: bool = True


class WatchBase(BaseModel):
    label: str = ""
    provider: str = "amiami"
    kind: WatchKind = WatchKind.search
    query: str = ""
    item_code: str | None = None
    filters: dict = {}
    condition: Condition = Condition.any
    stock_filter: StockFilter = StockFilter.any
    min_item_grade: Literal["S", "A", "B+", "B", "C", "D"] | None = None
    min_box_grade: Literal["S", "A", "B+", "B", "C", "D"] | None = None
    target_price: float | None = Field(default=None, ge=0)
    price_basis: PriceBasis = PriceBasis.listed
    target_currency: str = "JPY"
    notify_on_price_below: bool = True
    notify_on_restock: bool = True
    notify_on_new_match: bool = True
    notify_on_price_drop_pct: float | None = Field(default=None, ge=1, le=99)
    enabled: bool = True
    interval_seconds: int | None = Field(default=None, ge=15, le=86400)
    adaptive: bool = True
    priority: int = Field(default=0, ge=0, le=3)
    quiet_hours: QuietHours = QuietHours()
    cooldown_seconds: int = Field(default=1800, ge=0, le=604800)
    max_alerts_per_day: int = Field(default=50, ge=1, le=1000)
    channel_ids: list[int] = []


class WatchCreate(WatchBase):
    pass


class WatchUpdate(BaseModel):
    label: str | None = None
    query: str | None = None
    filters: dict | None = None
    condition: Condition | None = None
    stock_filter: StockFilter | None = None
    min_item_grade: Literal["S", "A", "B+", "B", "C", "D"] | None = None
    min_box_grade: Literal["S", "A", "B+", "B", "C", "D"] | None = None
    target_price: float | None = None
    price_basis: PriceBasis | None = None
    target_currency: str | None = None
    notify_on_price_below: bool | None = None
    notify_on_restock: bool | None = None
    notify_on_new_match: bool | None = None
    notify_on_price_drop_pct: float | None = None
    enabled: bool | None = None
    interval_seconds: int | None = None
    adaptive: bool | None = None
    priority: int | None = None
    quiet_hours: QuietHours | None = None
    cooldown_seconds: int | None = None
    max_alerts_per_day: int | None = None
    channel_ids: list[int] | None = None


class WatchOut(ORMModel, WatchBase):
    id: int
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    run_count: int = 0
    alert_count: int = 0
    baselined: bool = False
    created_at: datetime
    effective_interval_seconds: int | None = None
    match_count: int = 0
    recent_items: list[ItemOut] = []


# ---------------------------------------------------------------------------
# Alerts, channels, collection
# ---------------------------------------------------------------------------


class AlertOut(ORMModel):
    id: int
    watch_id: int | None
    item_id: int | None
    trigger: TriggerType
    title: str
    body: str
    price: float | None
    currency: str
    landed_price: float | None
    landed_currency: str
    previous_price: float | None
    url: str | None
    image_url: str | None
    extra: dict = {}
    read_at: datetime | None
    created_at: datetime
    watch_label: str | None = None
    delivery_summary: dict[str, int] = {}


class ChannelCreate(BaseModel):
    type: ChannelType
    name: str = ""
    config: dict = {}
    enabled: bool = True
    is_default: bool = True
    send_digest: bool = False


class ChannelUpdate(BaseModel):
    name: str | None = None
    config: dict | None = None
    enabled: bool | None = None
    is_default: bool | None = None
    send_digest: bool | None = None


class ChannelOut(ORMModel):
    id: int
    type: ChannelType
    name: str
    enabled: bool
    is_default: bool
    send_digest: bool
    last_used_at: datetime | None
    last_error: str | None
    failure_count: int
    created_at: datetime
    config_preview: dict = {}


class CollectionCreate(BaseModel):
    provider: str = "amiami"
    item_code: str | None = None
    item_id: int | None = None
    status: CollectionStatus = CollectionStatus.wishlist
    priority: int = Field(default=2, ge=1, le=3)
    notes: str = ""
    tags: list[str] = []
    paid_price: float | None = None
    paid_currency: str = "JPY"
    quantity: int = Field(default=1, ge=1, le=99)
    mfc_url: str | None = None


class CollectionUpdate(BaseModel):
    status: CollectionStatus | None = None
    priority: int | None = Field(default=None, ge=1, le=3)
    notes: str | None = None
    tags: list[str] | None = None
    paid_price: float | None = None
    paid_currency: str | None = None
    purchased_at: datetime | None = None
    quantity: int | None = Field(default=None, ge=1, le=99)
    mfc_url: str | None = None


class CollectionOut(ORMModel):
    id: int
    status: CollectionStatus
    priority: int
    notes: str
    tags: list[str] = []
    paid_price: float | None
    paid_currency: str
    purchased_at: datetime | None
    quantity: int
    mfc_url: str | None
    created_at: datetime
    updated_at: datetime
    item: ItemOut


# ---------------------------------------------------------------------------
# Dashboard, admin, misc
# ---------------------------------------------------------------------------


class DashboardStats(BaseModel):
    watches_active: int
    watches_total: int
    alerts_24h: int
    alerts_7d: int
    alerts_unread: int
    items_tracked: int
    wishlist_count: int
    collection_value: float | None = None
    collection_currency: str = "EUR"
    next_check_at: datetime | None = None
    cheapest_wishlist: list[ItemOut] = []
    recent_alerts: list[AlertOut] = []
    price_drops_7d: int = 0


class ProviderInfo(BaseModel):
    id: str
    name: str
    home_url: str
    currency: str
    description: str
    supports_facets: bool
    healthy: bool = True
    circuit: dict = {}
    rate_per_minute: int = 0
    last_latency_ms: float = 0.0


class SystemStatus(BaseModel):
    version: str
    scheduler: dict
    providers: list[ProviderInfo]
    fx: dict
    database: str
    users: int
    watches: int
    items: int
    price_points: int
    alerts: int
    data_dir: str
    webpush_configured: bool
    smtp_configured: bool
    registration_open: bool
    uptime_seconds: float


class PublicConfig(BaseModel):
    app_name: str
    registration_open: bool
    has_users: bool
    vapid_public_key: str | None = None
    providers: list[ProviderInfo] = []
    channel_types: list[dict] = []
    default_currency: str
    min_poll_interval_seconds: int
    version: str


class MessageResponse(BaseModel):
    ok: bool = True
    message: str = ""
    detail: Any | None = None


class ResolveRequest(BaseModel):
    input: str = Field(description="A shop URL or bare product code")
