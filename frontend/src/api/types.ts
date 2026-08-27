export type Condition = 'any' | 'new' | 'preowned'
export type StockFilter = 'any' | 'in_stock' | 'preorder' | 'backorder'
export type PriceBasis = 'listed' | 'landed'
export type WatchKind = 'search' | 'item'
export type CollectionStatus = 'wishlist' | 'ordered' | 'owned' | 'sold'
export type ChannelType =
  | 'telegram'
  | 'webpush'
  | 'email'
  | 'discord'
  | 'ntfy'
  | 'gotify'
  | 'webhook'
export type TriggerType =
  | 'price_below'
  | 'back_in_stock'
  | 'new_match'
  | 'price_drop'
  | 'restock_preowned'
  | 'deal_radar'

export interface CostBreakdown {
  currency: string
  goods: number
  shipping: number
  duty: number
  vat: number
  handling: number
  total: number
  weight_grams: number
  fx_rate: number | null
  duty_rate: number
  vat_rate: number
  duty_waived: boolean
  notes: string[]
}

export interface TagRef {
  id?: number
  kind: string
  slug: string
  name: string
  usage_count?: number
  is_auto?: boolean
  mfc_url?: string | null
}

export type Grade = 'S' | 'A' | 'B+' | 'B' | 'C' | 'D'

/** One graded copy on offer under a single product code. */
export interface ItemVariant {
  code: string
  price: number
  condition: string
  item_grade: Grade | null
  box_grade: Grade | null
}

export interface Item {
  id: number | null
  provider: string
  code: string
  name: string
  name_jp?: string | null
  url: string | null
  currency: string
  price: number | null
  price_max: number | null
  variants: ItemVariant[]
  list_price: number | null
  image_url: string | null
  images: string[]
  maker: string | null
  series: string | null
  character: string | null
  scale: string | null
  jan_code?: string | null
  spec?: string | null
  remarks?: string | null
  condition: string
  condition_grade: string | null
  in_stock: boolean
  is_preorder: boolean
  is_backorder: boolean
  order_closed: boolean
  release_date: string | null
  discount_pct: number | null
  landed: CostBreakdown | null
  display_price: number | null
  display_currency: string | null
  lowest_price: number | null
  highest_price: number | null
  average_price: number | null
  first_seen_at: string | null
  last_seen_at: string | null
  tracked: boolean
  watch_count: number
  in_collection: CollectionStatus | null
  collection_entry_id: number | null
  mfc_id: number | null
  mfc_url: string | null
  mfc_matched_by: string | null
  mfc_confidence: number | null
  mfc_restricted: boolean
  /** The figure is on a list under its other condition, not this listing. */
  saved_via_counterpart: boolean
  dwell_days: number | null
  dwell_basis: DwellBasis | null
  dwell_samples: number
  listing_count: number
  /** The same figure listed under the other condition, when known. */
  counterpart: {
    id: number
    code: string
    condition: string
    price: number | null
    currency: string
    in_stock: boolean
  } | null
  tags: TagRef[]
}

export interface User {
  id: number
  email: string
  username: string
  role: 'admin' | 'user'
  is_active: boolean
  display_currency: string
  theme: string
  color_mode: string
  timezone: string
  prefs: Record<string, any>
  created_at: string
  last_login_at: string | null
}

export interface QuietHours {
  enabled: boolean
  start: string
  end: string
  urgent_override: boolean
}

export interface Watch {
  id: number
  label: string
  provider: string
  kind: WatchKind
  query: string
  item_code: string | null
  filters: Record<string, any>
  condition: Condition
  stock_filter: StockFilter
  min_item_grade: Grade | null
  min_box_grade: Grade | null
  target_price: number | null
  price_basis: PriceBasis
  target_currency: string
  notify_on_price_below: boolean
  notify_on_restock: boolean
  notify_on_new_match: boolean
  notify_on_price_drop_pct: number | null
  enabled: boolean
  interval_seconds: number | null
  adaptive: boolean
  priority: number
  quiet_hours: QuietHours
  cooldown_seconds: number
  max_alerts_per_day: number
  channel_ids: number[]
  next_run_at: string | null
  last_run_at: string | null
  last_success_at: string | null
  last_error: string | null
  consecutive_errors: number
  run_count: number
  alert_count: number
  baselined: boolean
  created_at: string
  effective_interval_seconds: number | null
  match_count: number
  recent_items: Item[]
}

export interface Alert {
  /** Every reason this alert qualified, most important first. */
  reasons: string[]
  id: number
  watch_id: number | null
  item_id: number | null
  trigger: TriggerType
  title: string
  body: string
  price: number | null
  currency: string
  landed_price: number | null
  landed_currency: string
  previous_price: number | null
  url: string | null
  image_url: string | null
  extra: Record<string, any>
  read_at: string | null
  created_at: string
  watch_label: string | null
  delivery_summary: Record<string, number>
}

export interface Channel {
  id: number
  type: ChannelType
  name: string
  enabled: boolean
  is_default: boolean
  send_digest: boolean
  last_used_at: string | null
  last_error: string | null
  failure_count: number
  created_at: string
  config_preview: Record<string, any>
}

export interface ChannelFieldSpec {
  name: string
  label: string
  type: 'text' | 'password' | 'boolean' | 'number' | 'select' | 'textarea' | 'hidden'
  required?: boolean
  help?: string
  options?: string[]
  default?: any
}

export interface ChannelTypeInfo {
  type: ChannelType
  label: string
  fields: ChannelFieldSpec[]
  docs_url: string
  available: boolean
  unavailable_reason: string
}

export interface CollectionEntry {
  id: number
  status: CollectionStatus
  priority: number
  notes: string
  tags: string[]
  paid_price: number | null
  paid_currency: string
  purchased_at: string | null
  quantity: number
  mfc_url: string | null
  created_at: string
  updated_at: string
  item: Item
}

export type ShippingZone = 'zone1' | 'zone2' | 'zone3' | 'zone4' | 'zone5'

export type ShippingService =
  | 'auto_air'
  | 'auto'
  | 'small_packet'
  | 'small_packet_registered'
  | 'surface_parcel'
  | 'air_parcel'
  | 'ems'

export interface ShippingOptions {
  zones: { value: ShippingZone; label: string }[]
  services: { value: Exclude<ShippingService, 'auto' | 'auto_air'>; label: string }[]
}

export interface CostProfilePreview {
  sample_price: number
  sample_currency: string
  weight_grams: number
  packaging_grams: number
  breakdown: CostBreakdown | null
}

export interface CostProfile {
  country: string
  vat_rate: number
  duty_rate: number
  duty_free_threshold: number
  vat_free_threshold: number
  customs_handling_fee: number
  shipping_mode: 'amiami' | 'table' | 'flat' | 'none'
  shipping_zone: ShippingZone
  shipping_service: ShippingService
  shipping_flat: number
  shipping_table: { max_grams: number; cost: number }[]
  default_weight_grams: number
  packaging_grams: number
  weight_scale: number
  /** Words never shown while browsing, matched anywhere in a product name. */
  blocked_terms: string[]
  /** MyFigureCollection tag slugs never shown while browsing. */
  blocked_tags: string[]
  category_weights: Record<string, number>
  consolidate_shipping: boolean
  fx_markup: number
}

export interface DashboardStats {
  watches_active: number
  watches_total: number
  alerts_24h: number
  alerts_7d: number
  alerts_unread: number
  items_tracked: number
  wishlist_count: number
  collection_value: number | null
  collection_currency: string
  next_check_at: string | null
  cheapest_wishlist: Item[]
  recent_alerts: Alert[]
  price_drops_7d: number
}

export interface ProviderInfo {
  id: string
  name: string
  home_url: string
  currency: string
  description: string
  supports_facets: boolean
  healthy: boolean
  circuit: Record<string, any>
  rate_per_minute: number
  last_latency_ms: number
}

export interface PublicConfig {
  app_name: string
  registration_open: boolean
  has_users: boolean
  vapid_public_key: string | null
  providers: ProviderInfo[]
  channel_types: ChannelTypeInfo[]
  default_currency: string
  min_poll_interval_seconds: number
  version: string
  /** Opening an item asks the shop for fresh data. Instance-wide. */
  refresh_on_open: boolean
}

export interface SearchResponse {
  items: Item[]
  total: number
  page: number
  per_page: number
  pages: number
  facets: Record<string, { id: number; name: string; count: number | null }[]>
  took_ms: number
  provider: string
}

export interface PricePoint {
  recorded_at: string
  price: number | null
  currency: string
  in_stock: boolean
  sale_status: string | null
  condition_grade: string | null
}

export type DwellBasis = 'observed' | 'intake' | 'intake_bootstrap' | 'product'

export type ListingStatus = 'live' | 'gone'

export interface ListingLifetime {
  certain_days: number
  /** Null when either end of the copy's spell is unknown. */
  max_days: number | null
  /** It was already on the shelf the first time we looked. */
  open_start: boolean
  /** It is still on the shelf now. */
  open_end: boolean
  observations: number
}

export interface ListingRow {
  code: string
  sequence: number | null
  price: number | null
  last_price: number | null
  currency: string
  condition: string | null
  item_grade: string | null
  box_grade: string | null
  status: ListingStatus
  outcome: 'sold' | 'delisted' | 'withdrawn' | 'unknown' | null
  first_seen_at: string
  last_seen_at: string
  vanished_before: string | null
  lifetime: ListingLifetime
}

export interface ShelfLife {
  listings: ListingRow[]
  live_count: number
  observed_count: number
  departed_count: number
  anchored_count: number
  median_days: number | null
  by_grade: { grade: string; median_days: number; samples: number }[]
  intake_per_month: number | null
  intake_basis: 'measured' | 'bootstrap' | null
  intake_total: number | null
  intake_dwell_days: number | null
  dwell_days: number | null
  dwell_basis: DwellBasis | null
  sold_out_days: number | null
  cheapest_first: { wins: number; of: number } | null
  tracked_since: string | null
}

export interface ItemHistory {
  item: Item
  points: PricePoint[]
  stats: {
    lowest: number | null
    highest: number | null
    average: number | null
    points: number
    tracked_since: string | null
  }
}

export interface MessageResponse<T = any> {
  ok: boolean
  message: string
  detail: T
}

export interface SystemStatus {
  version: string
  scheduler: Record<string, any>
  providers: ProviderInfo[]
  fx: { base: string; rates: Record<string, number>; source: string | null; age_seconds: number | null; stale: boolean }
  database: string
  users: number
  watches: number
  items: number
  price_points: number
  alerts: number
  data_dir: string
  webpush_configured: boolean
  smtp_configured: boolean
  registration_open: boolean
  uptime_seconds: number
}
