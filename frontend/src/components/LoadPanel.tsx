import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'
import { Icon } from '@/components/Icon'
import { Card, SegmentedControl, Spinner } from '@/components/ui'
import clsx from 'clsx'

/**
 * Where the request budget is actually going, right now.
 *
 * Four jobs share one allowance to each shop: the catalogue sweep, the
 * shelf-life sampler, the watch poller and the MyFigureCollection linker,
 * plus photo downloads. Each has its own rate setting, but those say what a
 * job is *permitted*, and the interesting number is what it took. When a
 * sweep crawls slower than its settings suggest, this is the panel that says
 * whether something else was holding the budget.
 *
 * The window is a rolling one held in memory, so a restart empties it.
 */

const HOST_LABELS: Record<string, string> = {
  amiami: 'AmiAmi',
  mfc: 'MyFigureCollection',
}

/** Distinct enough to tell apart in a stacked bar, muted enough to read. */
const TONES = [
  'bg-accent',
  'bg-positive',
  'bg-warning',
  'bg-info',
  'bg-danger',
  'bg-faint',
]

type Purpose = {
  key: string
  label: string
  requests: number
  per_minute: number
  share: number
}

type Host = {
  total: number
  errors: number
  per_minute: number
  per_hour: number
  purposes: Purpose[]
}

const WINDOWS = [
  { value: '60', label: '1 min' },
  { value: '300', label: '5 min' },
  { value: '900', label: '15 min' },
  { value: '3600', label: '1 hour' },
]

export function LoadPanel() {
  const [window, setWindow] = useState('300')
  const seconds = Number(window)

  const load = useQuery({
    queryKey: ['admin', 'load', seconds],
    queryFn: () => api.admin.load(seconds),
    // Often enough to feel live without being its own source of traffic.
    refetchInterval: 10_000,
  })

  const d = load.data?.detail as
    | {
        window_seconds: number
        sampled: number
        held: number
        hourly_is_projected: boolean
        hosts: Record<string, Host>
        budgets: Record<string, number>
      }
    | undefined

  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <Icon name="chart" className="h-4 w-4 text-accent" />
            Requests going out
            {load.isFetching && <Spinner className="h-3 w-3" />}
          </h3>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-muted">
            Every outbound request in the last few minutes, split by the job that made it.
            Both shops have one shared allowance, so a slow sweep usually means something
            else was using it rather than the shop being slow.
          </p>
        </div>
        <SegmentedControl value={window} onChange={setWindow} options={WINDOWS} />
      </div>

      {d && d.sampled === 0 && (
        <p className="mt-4 rounded-control border border-line bg-raised p-3 text-xs text-faint">
          Nothing in the last {describe(d.window_seconds)}. Either the jobs are between runs
          or the server restarted — this counter is kept in memory and starts empty.
        </p>
      )}

      {d && d.sampled > 0 && (
        <div className="mt-4 space-y-4">
          {Object.entries(d.hosts).map(([host, info]) => (
            <HostRow
              key={host}
              host={host}
              info={info}
              budget={d.budgets?.[host]}
              projected={d.hourly_is_projected}
            />
          ))}
          <p className="text-[11px] leading-relaxed text-faint">
            {d.hourly_is_projected
              ? `The hourly column is this ${describe(d.window_seconds)} scaled up, not an hour that
                 has happened. Pick the one-hour window for a figure that was actually measured.`
              : 'Measured over a full hour.'}{' '}
            {d.held.toLocaleString('en-GB')} requests are being held in all; anything older
            than an hour is dropped.
          </p>
        </div>
      )}
    </Card>
  )
}

function HostRow({
  host,
  info,
  budget,
  projected,
}: {
  host: string
  info: Host
  budget?: number
  projected: boolean
}) {
  // The share of the allowance in use. Over 100% is possible and worth
  // seeing: the limiter paces the crawler, but a page opened by hand or a
  // retry does not queue behind it.
  const used = budget ? (info.per_minute / budget) * 100 : null

  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-xs font-medium">
          {HOST_LABELS[host] ?? host}
          {info.errors > 0 && (
            <span className="ml-2 text-danger">
              {info.errors.toLocaleString('en-GB')} failed
            </span>
          )}
        </p>
        <p className="text-xs tabular-nums">
          <span className="font-medium">{info.per_minute.toLocaleString('en-GB')}</span>
          <span className="text-faint"> /min</span>
          <span className="mx-1.5 text-faint">·</span>
          <span className="font-medium">
            {projected && '~'}
            {info.per_hour.toLocaleString('en-GB')}
          </span>
          <span className="text-faint"> /h</span>
          {used !== null && (
            <span className={clsx('ml-2', used > 95 ? 'text-warning' : 'text-faint')}>
              {used.toFixed(0)}% of {budget}/min
            </span>
          )}
        </p>
      </div>

      <div className="mt-1.5 flex h-2 overflow-hidden rounded-full bg-raised">
        {info.purposes.map((p, i) => (
          <div
            key={p.key}
            className={clsx(TONES[i % TONES.length], 'h-full')}
            style={{ width: `${p.share}%` }}
            title={`${p.label}: ${p.requests} requests`}
          />
        ))}
      </div>

      <dl className="mt-2 grid gap-x-6 gap-y-1 text-[11px] sm:grid-cols-2">
        {info.purposes.map((p, i) => (
          <div key={p.key} className="flex items-baseline justify-between gap-2">
            <dt className="flex min-w-0 items-center gap-1.5 text-muted">
              <span
                className={clsx(TONES[i % TONES.length], 'h-2 w-2 shrink-0 rounded-full')}
              />
              <span className="truncate">{p.label}</span>
            </dt>
            <dd className="shrink-0 tabular-nums">
              {p.per_minute.toLocaleString('en-GB')}/min
              <span className="ml-1.5 text-faint">{p.share.toFixed(0)}%</span>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

function describe(seconds: number): string {
  if (seconds >= 3600) return `${Math.round(seconds / 3600)} hour(s)`
  if (seconds >= 60) return `${Math.round(seconds / 60)} minutes`
  return `${seconds} seconds`
}
