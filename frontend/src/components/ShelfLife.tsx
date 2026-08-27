import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { DwellBasis, ListingRow } from '@/api/types'
import { Icon } from '@/components/Icon'
import { Card, Spinner, Tooltip } from '@/components/ui'
import { money, shortDate } from '@/lib/format'
import clsx from 'clsx'

/**
 * Every copy we have seen under one product code, on a shared time axis.
 *
 * AmiAmi deletes a pre-owned copy the moment it sells, so a copy missing from
 * a later look is a sale. What we cannot see is exactly when it happened: the
 * answer sits between the last look that found it and the first that did not.
 * Every figure here shows that bracket rather than a false precision.
 */

function days(value: number): string {
  if (value < 1) return 'under a day'
  if (value < 2) return '1 day'
  return `${Math.round(value)} days`
}

/** How much a headline number deserves to be trusted, in plain words. */
const BASIS_NOTE: Record<DwellBasis, string> = {
  observed: 'measured from copies we watched sell here',
  intake: "derived from how fast the shop restocks this figure, not from copies we watched",
  intake_bootstrap:
    'a rough guess from the shop’s restock count and this figure’s age — treat it as a hint',
  product: 'from how long the whole listing stood last time, not from individual copies',
}

function Bar({ row, span }: { row: ListingRow; span: { start: number; end: number } }) {
  const total = Math.max(1, span.end - span.start)
  const from = new Date(row.first_seen_at).getTime()
  const to = row.vanished_before ? new Date(row.vanished_before).getTime() : span.end

  const left = ((Math.max(from, span.start) - span.start) / total) * 100
  const width = Math.max(1.5, ((Math.min(to, span.end) - Math.max(from, span.start)) / total) * 100)
  const live = row.status === 'live'
  const life = row.lifetime

  const label = live
    ? `still listed · ${days(life.certain_days)}`
    : life.open_start
      ? `at least ${days(life.certain_days)}`
      : life.max_days && life.max_days - life.certain_days > 0.5
        ? `${days(life.certain_days)}–${days(life.max_days)}`
        : days(life.certain_days)

  const detail = (
    <div className="space-y-1 text-xs">
      <p className="font-semibold">{row.code}</p>
      <p>First seen {shortDate(row.first_seen_at)}</p>
      {row.vanished_before ? (
        <p>
          Gone between {shortDate(row.last_seen_at)} and {shortDate(row.vanished_before)}
        </p>
      ) : (
        <p>Last confirmed {shortDate(row.last_seen_at)}</p>
      )}
      {life.open_start && (
        <p className="text-warning">Already listed when we first looked, so it ran longer than this.</p>
      )}
      {row.outcome === 'withdrawn' && (
        <p className="text-warning">Vanished together with every other copy, so possibly withdrawn.</p>
      )}
      <p className="text-faint">Seen on {life.observations} check(s)</p>
    </div>
  )

  return (
    <Tooltip content={detail}>
      <div className="relative h-5 w-full cursor-default">
        <div
          className={clsx(
            'absolute top-1 h-3 rounded-sm border',
            live ? 'border-positive bg-positive/20' : 'border-accent bg-accent/20',
          )}
          style={{ left: `${left}%`, width: `${width}%` }}
        >
          {life.open_start && (
            // It was already there when we arrived, so the bar starts mid-air.
            <span className="absolute -left-2 top-1/2 -translate-y-1/2 text-2xs text-faint">
              &lsaquo;
            </span>
          )}
          {!live && (
            <span className="absolute -right-1 top-1/2 h-2 w-2 -translate-y-1/2 rounded-full bg-accent" />
          )}
        </div>
        <span
          className="absolute top-0.5 whitespace-nowrap text-2xs text-faint"
          style={
            left + width > 62
              ? { right: `${Math.max(0, 100 - left)}%`, marginRight: '0.4rem' }
              : { left: `${left + width}%`, marginLeft: '0.4rem' }
          }
        >
          {label}
        </span>
      </div>
    </Tooltip>
  )
}

export function ShelfLifePanel({ itemId, preowned }: { itemId: number; preowned: boolean }) {
  const query = useQuery({
    queryKey: ['shelfLife', itemId],
    queryFn: () => api.items.shelfLife(itemId),
    enabled: preowned,
  })

  const data = query.data
  const span = useMemo(() => {
    if (!data?.listings.length) return null
    const times = data.listings.flatMap((row) => [
      new Date(row.first_seen_at).getTime(),
      row.vanished_before ? new Date(row.vanished_before).getTime() : Date.now(),
    ])
    const start = Math.min(...times)
    const end = Math.max(...times, Date.now())
    // A single same-day observation would collapse to zero width.
    return end - start < 3600_000 ? { start, end: start + 86_400_000 } : { start, end }
  }, [data])

  if (!preowned) return null
  if (query.isLoading) {
    return (
      <Card className="p-4">
        <Spinner className="h-5 w-5" />
      </Card>
    )
  }
  if (!data) return null

  const headline = data.dwell_days
  const basis = data.dwell_basis

  return (
    <Card className="p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <Icon name="clock" className="h-4 w-4 text-accent" />
          Shelf life
        </h2>
        {data.observed_count > 0 && (
          <span className="text-2xs text-faint">
            {data.live_count} listed now · {data.departed_count} sold since we started watching
          </span>
        )}
      </div>

      {headline ? (
        <div className="mb-4 rounded-card border border-line bg-raised p-3">
          <p className="text-sm">
            Copies of this figure typically last{' '}
            <span className="font-semibold text-accent">{days(headline)}</span> on the shelf.
          </p>
          <p className="mt-0.5 text-2xs text-faint">
            {basis ? BASIS_NOTE[basis] : ''}
            {basis === 'observed' && data.anchored_count > 0
              ? ` (${data.anchored_count} with a known start)`
              : ''}
          </p>
          {data.by_grade.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 border-t border-line pt-2 text-2xs">
              {data.by_grade.map((row) => (
                <span key={row.grade} className="text-muted">
                  Item {row.grade}:{' '}
                  <span className="font-semibold text-ink">{days(row.median_days)}</span>{' '}
                  <span className="text-faint">({row.samples})</span>
                </span>
              ))}
            </div>
          )}
          {data.cheapest_first && data.cheapest_first.of >= 3 && (
            <p className="mt-2 border-t border-line pt-2 text-2xs text-muted">
              The cheapest copy on offer was the one that sold in{' '}
              <span className="font-semibold text-ink">
                {data.cheapest_first.wins} of {data.cheapest_first.of}
              </span>{' '}
              cases.
            </p>
          )}
        </div>
      ) : (
        <p className="mb-4 text-sm text-muted">
          Not enough history yet to say how long copies last. Every check adds to it.
        </p>
      )}

      {span && data.listings.length > 0 ? (
        <>
          <div className="space-y-1.5">
            {data.listings.map((row) => (
              <div key={row.code} className="grid grid-cols-[5.5rem_1fr] items-center gap-2 sm:grid-cols-[5.5rem_7rem_5rem_1fr]">
                <span className="truncate font-mono text-2xs font-semibold" title={row.code}>
                  {row.sequence !== null ? `#${row.sequence}` : row.code}
                </span>
                <span className="hidden truncate text-2xs text-muted sm:block">
                  {row.item_grade ? `Item ${row.item_grade} / Box ${row.box_grade ?? '?'}` : '—'}
                </span>
                <span className="hidden text-right font-mono text-2xs tabular-nums text-muted sm:block">
                  {money(row.last_price ?? row.price, row.currency)}
                </span>
                <Bar row={row} span={span} />
              </div>
            ))}
          </div>
          <div className="mt-2 flex justify-between border-t border-line pt-1.5 text-2xs text-faint">
            <span>{shortDate(new Date(span.start).toISOString())}</span>
            <span>today</span>
          </div>
        </>
      ) : (
        <p className="text-sm text-muted">
          No individual copies recorded yet. They are picked up the next time this product is
          checked in detail.
        </p>
      )}

      {data.intake_total !== null && (
        <p className="mt-3 border-t border-line pt-2 text-2xs text-faint">
          The shop has taken in about {data.intake_total} used copies of this figure
          {data.intake_per_month ? `, currently around ${data.intake_per_month} a month` : ''}.
          {data.intake_basis === 'bootstrap' && ' Estimated from its release date until we have watched the count move.'}
        </p>
      )}
    </Card>
  )
}
