import clsx from 'clsx'
import { useState } from 'react'
import type { Item } from '@/api/types'
import { money, percent, tidyName } from '@/lib/format'
import { useTheme } from '@/lib/theme'
import { Badge, Tooltip } from './ui'
import { Icon } from './Icon'

/**
 * How quickly copies of this figure disappear, when we know.
 *
 * Only shown once it is short enough to matter. A badge saying "sells in 40
 * days" is noise on a grid; one saying "gone in 3" changes what you do next.
 */
function ShelfBadge({ item }: { item: Item }) {
  const days = item.dwell_days
  if (days === null || days > 14 || item.order_closed) return null

  const label = days < 1 ? 'Gone in hours' : days < 2 ? 'Gone in a day' : `Gone in ~${Math.round(days)}d`
  const firm = item.dwell_basis === 'observed'
  const explain = firm
    ? `Measured from ${item.dwell_samples} copy(ies) we watched sell here.`
    : 'Estimated from how fast the shop restocks this figure.'

  return (
    <Tooltip content={<span className="text-xs">{explain}</span>}>
      <Badge tone={days <= 3 ? 'danger' : 'warning'}>
        <Icon name="clock" className="h-3 w-3" />
        {label}
      </Badge>
    </Tooltip>
  )
}


function StockBadge({ item }: { item: Item }) {
  if (item.order_closed) return <Badge tone="danger">Sold out</Badge>
  if (item.in_stock) return <Badge tone="positive">In stock</Badge>
  if (item.is_preorder) return <Badge tone="info">Pre-order</Badge>
  if (item.is_backorder) return <Badge tone="warning">Back-order</Badge>
  return <Badge>Unavailable</Badge>
}

export function LandedTooltip({ item }: { item: Item }) {
  const landed = item.landed
  if (!landed) return null
  const rows: [string, number][] = [
    ['Goods', landed.goods],
    ['Shipping', landed.shipping],
    ['Customs duty', landed.duty],
    ['Import VAT', landed.vat],
    ['Handling fee', landed.handling],
  ]
  return (
    <div className="w-56 space-y-1 text-left">
      {rows.map(([label, value]) => (
        <div key={label} className="flex justify-between gap-4">
          <span className="text-muted">{label}</span>
          <span className="font-medium tabular-nums">{money(value, landed.currency)}</span>
        </div>
      ))}
      <div className="mt-1.5 flex justify-between gap-4 border-t border-line pt-1.5 font-semibold">
        <span>Estimated total</span>
        <span className="tabular-nums">{money(landed.total, landed.currency)}</span>
      </div>
      {landed.notes.length > 0 && (
        <p className="pt-1 text-[11px] leading-relaxed text-faint">{landed.notes.join('. ')}.</p>
      )}
    </div>
  )
}

export interface ItemCardProps {
  item: Item
  onOpen?: (item: Item) => void
  onWatch?: (item: Item) => void
  onWishlist?: (item: Item) => void
  compact?: boolean
}

export function ItemCard({ item, onOpen, onWatch, onWishlist, compact }: ItemCardProps) {
  const [imageFailed, setImageFailed] = useState(false)
  const { cardShape } = useTheme()
  const discount = item.discount_pct
  const preowned = item.condition === 'preowned'

  return (
    <article
      className={clsx(
        'card card-hover group relative flex flex-col overflow-hidden',
        onOpen && 'cursor-pointer',
      )}
      onClick={() => onOpen?.(item)}
    >
      {/* AmiAmi's photos are square. Square shows all of one; portrait crops
          it but fits more figures on a screen. */}
      <div
        className={clsx(
          'relative overflow-hidden bg-raised',
          cardShape === 'square' ? 'aspect-square' : 'aspect-[3/4]',
        )}
      >
        {item.image_url && !imageFailed ? (
          <img
            src={item.image_url}
            alt=""
            loading="lazy"
            decoding="async"
            onError={() => setImageFailed(true)}
            className={clsx(
              'h-full w-full transition-transform duration-500 group-hover:scale-[1.04]',
              // Square matches the source, so nothing needs cropping there.
              cardShape === 'square' ? 'object-contain' : 'object-cover',
            )}
          />
        ) : (
          <div className="grid h-full w-full place-items-center text-faint">
            <Icon name="box" className="h-8 w-8" />
          </div>
        )}

        <div className="absolute left-2 top-2 flex flex-col items-start gap-1">
          {preowned && <Badge tone="accent">Pre-owned</Badge>}
          {discount !== null && discount >= 10 && (
            <Badge tone="positive">-{Math.round(discount)}%</Badge>
          )}
          <ShelfBadge item={item} />
        </div>

        <div className="absolute right-2 top-2 flex flex-col items-end gap-1">
          <StockBadge item={item} />
          {item.watch_count > 0 && (
            <Badge tone="info">
              <Icon name="eye" className="h-3 w-3" />
              {item.watch_count}
            </Badge>
          )}
        </div>

        {/* Quick actions slide in on hover; on touch they are always visible. */}
        {(onWatch || onWishlist) && (
          <div className="absolute inset-x-2 bottom-2 flex gap-1.5 opacity-0 transition-opacity duration-200 group-hover:opacity-100 focus-within:opacity-100 max-md:opacity-100">
            {onWatch && (
              <button
                onClick={(event) => {
                  event.stopPropagation()
                  onWatch(item)
                }}
                className="btn flex-1 bg-surface/95 py-1.5 text-xs backdrop-blur hover:bg-surface"
              >
                <Icon name="bell" className="h-3.5 w-3.5" />
                Track
              </button>
            )}
            {onWishlist && (
              <button
                onClick={(event) => {
                  event.stopPropagation()
                  onWishlist(item)
                }}
                aria-pressed={Boolean(item.in_collection)}
                title={item.in_collection ? 'Remove from your wishlist' : 'Add to your wishlist'}
                aria-label={item.in_collection ? 'Remove from wishlist' : 'Add to wishlist'}
                className={clsx(
                  'btn px-2.5 py-1.5 backdrop-blur',
                  item.in_collection
                    ? 'bg-accent text-accent-ink hover:brightness-110'
                    : 'bg-surface/95 hover:bg-surface',
                )}
              >
                <Icon
                  name="heart"
                  className={clsx('h-3.5 w-3.5', item.in_collection && 'fill-current')}
                />
              </button>
            )}
          </div>
        )}
      </div>

      <div className="flex flex-1 flex-col gap-2 p-3">
        <h3
          className={clsx(
            'font-medium leading-snug text-balance',
            compact ? 'line-clamp-2 text-xs' : 'line-clamp-2 text-[13px]',
          )}
          title={item.name}
        >
          {tidyName(item.name)}
        </h3>

        {!compact && (item.maker || item.series) && (
          <p className="-mt-1 truncate text-[11px] text-faint">{item.series || item.maker}</p>
        )}

        <div className="mt-auto">
          <div className="flex items-baseline gap-2">
            <span className="text-base font-semibold tabular-nums">
              {money(item.price, item.currency)}
            </span>
            {item.list_price && discount !== null && discount > 0 && (
              <span className="text-xs text-faint line-through tabular-nums">
                {money(item.list_price, item.currency)}
              </span>
            )}
          </div>

          {/* Several graded copies at once: the headline price is the cheapest
              of them, so say so rather than implying it is the only price. */}
          {item.price_max !== null && item.price !== null && item.price_max > item.price && (
            <p className="mt-0.5 text-[11px] text-faint">
              {item.variants.length > 1 ? `${item.variants.length} grades, ` : ''}up to{' '}
              {money(item.price_max, item.currency)}
            </p>
          )}

          {item.landed && (
            <Tooltip content={<LandedTooltip item={item} />}>
              <span className="mt-0.5 inline-flex cursor-help items-center gap-1 text-xs text-muted">
                <Icon name="box" className="h-3 w-3" />
                <span className="tabular-nums">
                  {money(item.landed.total, item.landed.currency)}
                </span>
                <span className="text-faint">landed</span>
              </span>
            </Tooltip>
          )}

          {item.lowest_price !== null && item.price !== null && item.lowest_price < item.price && (
            <p className="mt-0.5 text-[11px] text-faint">
              Lowest seen {money(item.lowest_price, item.currency)}
            </p>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-faint">
          {item.condition_grade && <span className="chip py-0.5">{item.condition_grade}</span>}
          {item.scale && <span className="chip py-0.5">{item.scale}</span>}
          {item.release_date && <span className="chip py-0.5">{item.release_date}</span>}
          {item.mfc_id && (
            <Tooltip
              content={
                item.mfc_matched_by === 'jan'
                  ? 'Matched to MyFigureCollection by barcode'
                  : `Probable MyFigureCollection match (${percent((item.mfc_confidence ?? 0) * 100)})`
              }
            >
              <span
                className={clsx(
                  'chip py-0.5',
                  item.mfc_matched_by === 'jan' && 'border-info/40 text-info',
                )}
              >
                MFC
              </span>
            </Tooltip>
          )}
        </div>
      </div>
    </article>
  )
}

export function ItemCardSkeleton() {
  return (
    <div className="card overflow-hidden">
      <div className="skeleton aspect-[3/4]" />
      <div className="space-y-2 p-3">
        <div className="skeleton h-3 w-full rounded" />
        <div className="skeleton h-3 w-2/3 rounded" />
        <div className="skeleton h-4 w-1/2 rounded" />
      </div>
    </div>
  )
}
