import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import { Icon } from '@/components/Icon'
import { Card, SegmentedControl, Spinner, Tooltip } from '@/components/ui'
import clsx from 'clsx'

/**
 * When the shop is actually busy.
 *
 * The polling schedule is currently uniform around the clock, which is the
 * safe default and the wasteful one: AmiAmi is a Japanese shop staffed during
 * Japanese business hours, so most of what there is to find arrives in a few
 * predictable hours and the rest of the day is spent asking a quiet server the
 * same question.
 *
 * Everything here is derived from timestamps already stored, so it works from
 * the first day rather than needing a new counter to fill up.
 */

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

type Activity = {
  days: number
  new_listings: number
  price_changes: number
  listings_by_hour_utc: number[]
  changes_by_hour_utc: number[]
  listings_by_weekday: number[]
  changes_by_weekday: number[]
}

/** UTC buckets shifted onto the reader's own clock. */
function toLocalHours(utc: number[]): number[] {
  const offsetHours = -new Date().getTimezoneOffset() / 60
  const whole = Math.round(offsetHours)
  return utc.map((_, index) => utc[(((index - whole) % 24) + 24) % 24])
}

/**
 * One bar per hour, with the hour written underneath it.
 *
 * An axis with a handful of numbers spread across the bottom looked tidy and
 * was useless: you could see that something peaked without being able to say
 * when. Every bar carries its own label now, and the busy ones are called out
 * in words below the chart, because that is the sentence someone actually
 * wants out of this.
 */
function HourBars({ values }: { values: number[] }) {
  const peak = Math.max(1, ...values)
  const total = values.reduce((sum, v) => sum + v, 0)
  return (
    <div className="flex items-end gap-[2px]">
      {values.map((value, hour) => {
        const share = total ? (value / total) * 100 : 0
        return (
          <Tooltip
            key={hour}
            content={
              <span className="text-xs">
                {String(hour).padStart(2, '0')}:00 &ndash;{' '}
                {String((hour + 1) % 24).padStart(2, '0')}:00 &middot;{' '}
                {value.toLocaleString('en-GB')} ({share.toFixed(1)}%)
              </span>
            }
          >
            <div className="flex flex-1 flex-col items-center gap-1">
              <span className="text-[9px] tabular-nums text-faint">
                {value >= peak * 0.6 ? value.toLocaleString('en-GB') : ''}
              </span>
              <div className="flex h-24 w-full items-end">
                <div
                  className={clsx(
                    'w-full rounded-sm transition-[height]',
                    value >= peak * 0.75
                      ? 'bg-accent'
                      : value >= peak * 0.4
                        ? 'bg-accent/60'
                        : 'bg-accent/25',
                  )}
                  style={{ height: `${Math.max(2, (value / peak) * 100)}%` }}
                />
              </div>
              <span
                className={clsx(
                  'text-[9px] tabular-nums',
                  value >= peak * 0.75 ? 'font-semibold text-accent' : 'text-faint',
                )}
              >
                {String(hour).padStart(2, '0')}
              </span>
            </div>
          </Tooltip>
        )
      })}
    </div>
  )
}

/** The longest run of hours with almost nothing in them. */
function quiet(values: number[]): string {
  const peak = Math.max(...values)
  if (!peak) return ''
  let best: number[] = []
  let run: number[] = []
  // Wrapped, because a quiet stretch usually spans midnight.
  for (let index = 0; index < 48; index += 1) {
    const hour = index % 24
    if (values[hour] <= peak * 0.15) {
      run.push(hour)
      if (run.length > best.length && run.length <= 24) best = [...run]
    } else {
      run = []
    }
  }
  if (best.length < 3) return ''
  const pad = (h: number) => `${String(h).padStart(2, '0')}:00`
  return `${pad(best[0])}–${pad((best[best.length - 1] + 1) % 24)}`
}

/** "08:00 to 11:00, and again at 15:00" rather than a single peak hour. */
function busyHours(values: number[]): string {
  const peak = Math.max(...values)
  if (!peak) return ''
  const busy = values.map((v, hour) => ({ v, hour })).filter((e) => e.v >= peak * 0.6)
  if (!busy.length) return ''
  const runs: number[][] = []
  for (const { hour } of busy) {
    const previous = runs[runs.length - 1]
    if (previous && hour === previous[previous.length - 1] + 1) previous.push(hour)
    else runs.push([hour])
  }
  const pad = (h: number) => `${String(h).padStart(2, '0')}:00`
  return runs
    .map((run) => (run.length > 1 ? `${pad(run[0])}–${pad(run[run.length - 1] + 1)}` : pad(run[0])))
    .join(', ')
}

export function ActivityPanel() {
  const [metric, setMetric] = useState<'listings' | 'changes'>('listings')
  const [days, setDays] = useState('30')

  const activity = useQuery({
    queryKey: ['admin', 'activity', days],
    queryFn: () => api.admin.activity(Number(days)),
  })

  const data = activity.data?.detail as Activity | undefined
  if (activity.isLoading) {
    return (
      <Card className="p-4">
        <Spinner className="h-5 w-5" />
      </Card>
    )
  }
  if (!data) return null

  const hourly = toLocalHours(
    metric === 'listings' ? data.listings_by_hour_utc : data.changes_by_hour_utc,
  )
  const weekly = metric === 'listings' ? data.listings_by_weekday : data.changes_by_weekday
  const total = metric === 'listings' ? data.new_listings : data.price_changes

  return (
    <Card className="p-4">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <Icon name="chart" className="h-4 w-4 text-accent" />
            When the shop is busy
          </h3>
          <p className="mt-0.5 max-w-xl text-xs text-muted">
            {total.toLocaleString('en-GB')}{' '}
            {metric === 'listings' ? 'new listings' : 'price changes'} over the last {data.days}{' '}
            days, by the hour on your own clock. Useful for deciding when the crawler should be
            working hard and when it can idle.
          </p>
        </div>
        <SegmentedControl
          value={metric}
          onChange={setMetric}
          options={[
            { value: 'listings', label: 'New listings' },
            { value: 'changes', label: 'Price changes' },
          ]}
        />
      </div>

      <HourBars values={hourly} />

      <div className="mt-4 border-t border-line pt-3">
        <p className="mb-2 text-xs text-faint">By day of week</p>
        <div className="flex items-end gap-2">
          {weekly.map((value, index) => {
            const peak = Math.max(1, ...weekly)
            return (
              <div key={index} className="flex flex-1 flex-col items-center gap-1">
                <div className="flex h-12 w-full items-end">
                  <div
                    className="w-full rounded-sm bg-accent/40"
                    style={{ height: `${Math.max(2, (value / peak) * 100)}%` }}
                  />
                </div>
                <span className="text-[10px] text-faint">{WEEKDAYS[index]}</span>
              </div>
            )
          })}
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center justify-between gap-3 border-t border-line pt-3">
        <p className="text-xs text-muted">
          {busyHours(hourly) ? (
            <>
              Busiest between{' '}
              <span className="font-semibold text-ink">{busyHours(hourly)}</span> your time
              {quiet(hourly) && (
                <>
                  , and quiet from{' '}
                  <span className="font-semibold text-ink">{quiet(hourly)}</span>
                </>
              )}
              .
            </>
          ) : (
            'Not enough recorded yet to see a pattern.'
          )}
        </p>
        <SegmentedControl
          value={days}
          onChange={setDays}
          options={[
            { value: '7', label: '7d' },
            { value: '30', label: '30d' },
            { value: '90', label: '90d' },
          ]}
        />
      </div>

      <p className="mt-2 text-[11px] leading-relaxed text-faint">
        This measures when <em>this instance</em> noticed something, not when the shop did it, so
        it is blurred by the polling interval and shows nothing for hours when nothing was
        polling. It is enough to find the daily rhythm, which is what it is for.
      </p>
    </Card>
  )
}
