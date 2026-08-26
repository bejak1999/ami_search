import type { IconName } from '@/components/Icon'
import type { TriggerType } from '@/api/types'

/** Presentation for each alert reason: label, glyph and colour. */
export const TRIGGER_META: Record<
  TriggerType,
  { label: string; icon: IconName; tone: 'accent' | 'positive' | 'info' | 'warning' | 'danger' }
> = {
  price_below: { label: 'Target reached', icon: 'yen', tone: 'positive' },
  price_drop: { label: 'Price dropped', icon: 'down', tone: 'positive' },
  back_in_stock: { label: 'Back in stock', icon: 'box', tone: 'info' },
  restock_preowned: { label: 'Pre-owned available', icon: 'refresh', tone: 'info' },
  new_match: { label: 'New match', icon: 'sparkle', tone: 'accent' },
  deal_radar: { label: 'Unusually cheap', icon: 'fire', tone: 'warning' },
}

export const TRIGGER_OPTIONS = (Object.keys(TRIGGER_META) as TriggerType[]).map((key) => ({
  value: key,
  label: TRIGGER_META[key].label,
}))
