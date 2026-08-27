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

function Bars({ values, labels }: { values: number[]; labels: string[] }) {
  const peak = Math.max(1, ...values)
  return (
    <div className="flex h-24 items-end gap-[3px]">
      {values.map((value, index) => (
        <Tooltip
          key={index}
          content={
            <span className="text-xs">
              {labels[index]}: {value.toLocaleString('en-GB')}
            </span>
          }
        >
          <div className="flex h-24 flex-1 items-end">
            <div
              className={clsx(
                'w-full rounded-sm transition-[height]',
                value >= peak * 0.75 ? 'bg-accent' : 'bg-accent/40',
              )}
              style={{ height: `${Math.max(2, (value / peak) * 100)}%` }}
            />
          </div>
        </Tooltip>
      ))}
    </div>
  )
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
  const hourLabels = hourly.map((_, hour) => `${String(hour).padStart(2, '0')}:00`)
  const busiest = hourly.indexOf(Math.max(...hourly))
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

      <Bars values={hourly} labels={hourLabels} />
      <div className="mt-1 flex justify-between text-[10px] text-faint">
        {[0, 6, 12, 18, 23].map((hour) => (
          <span key={hour}>{String(hour).padStart(2, '0')}</span>
        ))}
      </div>

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
          Busiest around{' '}
          <span className="font-semibold text-ink">
            {String(busiest).padStart(2, '0')}:00
          </span>{' '}
          your time.
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
