import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { useToast } from '@/lib/toast'
import { duration, relativeTime } from '@/lib/format'
import { ActivityPanel } from './ActivityPanel'
import { BackupPanel } from './BackupPanel'
import { LoadPanel } from './LoadPanel'
import { Icon } from './Icon'
import { Badge, Card, Field, SectionTitle, Spinner, Toggle } from './ui'
import clsx from 'clsx'

function Bar({ percent, tone = 'accent' }: { percent: number; tone?: 'accent' | 'positive' }) {
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-raised">
      <div
        className={clsx(
          'h-full rounded-full transition-[width] duration-500',
          tone === 'positive' ? 'bg-positive' : 'bg-accent-gradient',
        )}
        style={{ width: `${Math.max(0, Math.min(100, percent))}%` }}
      />
    </div>
  )
}

/** "in about 4 hours", or an em dash when there is nothing left to do. */
function eta(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  return `about ${duration(seconds)}`
}

function MfcSession() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [cookie, setCookie] = useState('')
  const [open, setOpen] = useState(false)

  const session = useQuery({
    queryKey: ['admin', 'mfcSession'],
    queryFn: () => api.admin.mfcSession(),
  })

  const save = useMutation({
    mutationFn: () => api.admin.setMfcSession(cookie),
    onSuccess: (result) => {
      if (result.ok) toast.success(result.message)
      else toast.error('Session not accepted', result.message)
      setCookie('')
      void queryClient.invalidateQueries({ queryKey: ['admin'] })
    },
    onError: (error) => toast.error('Could not save the session', (error as Error).message),
  })

  const recheck = useMutation({
    mutationFn: () => api.admin.recheckRestricted(50),
    onSuccess: (result) => {
      toast.success(result.message)
      void queryClient.invalidateQueries({ queryKey: ['admin'] })
    },
  })

  const state = session.data?.detail ?? {}

  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <Icon name="user" className="h-4 w-4 text-accent" />
            MyFigureCollection session
            {state.configured ? (
              <Badge tone={state.valid ? 'positive' : 'danger'}>
                {state.valid ? 'Signed in' : 'Not accepted'}
              </Badge>
            ) : (
              <Badge>Guest</Badge>
            )}
          </h3>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-muted">
            {state.detail ??
              'MyFigureCollection serves a plain 404 for entries it restricts to members, so those items can be identified by barcode but never tagged.'}
          </p>
        </div>
        <button onClick={() => setOpen((v) => !v)} className="btn-ghost shrink-0 text-xs">
          <Icon name={open ? 'chevronDown' : 'chevronRight'} className="h-3.5 w-3.5" />
          {state.configured ? 'Replace' : 'Set up'}
        </button>
      </div>

      {state.configured && !state.valid && state.cookies?.length ? (
        <p className="mt-3 text-xs text-faint">
          Cookies received: <span className="font-mono">{state.cookies.join(', ')}</span>
        </p>
      ) : null}

      {state.configured && state.restricted_entries_visible === false && (
        <p className="mt-3 flex items-start gap-2 rounded-control border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
          <Icon name="alertTriangle" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          Signed in, but restricted entries are still hidden. Turn on adult content in your
          MyFigureCollection account settings.
        </p>
      )}

      {open && (
        <div className="mt-4 space-y-3 border-t border-line pt-4">
          <ol className="list-decimal space-y-2 pl-4 text-xs leading-relaxed text-muted">
            <li>
              Sign in to MyFigureCollection, and tick <em>remember me</em>. Without it the
              session dies when you close the browser, and this stops working with it.
            </li>
            <li>
              Turn on adult content under{' '}
              <span className="font-mono">Settings → Account → Content</span> on
              MyFigureCollection. Being signed in is not enough on its own; that switch is what
              makes restricted entries visible.
            </li>
            <li>
              Press <kbd className="rounded border border-line bg-raised px-1">F12</kbd>, open{' '}
              <span className="font-mono">Storage</span> (Firefox) or{' '}
              <span className="font-mono">Application</span> (Chrome), expand{' '}
              <span className="font-mono">Cookies → https://myfigurecollection.net</span>, and
              copy the <strong>Value</strong> of the row named{' '}
              <span className="font-mono">PHPSESSID</span>.
            </li>
            <li>Paste it below. Just the value is fine; a whole cookie string works too.</li>
          </ol>

          <p className="flex items-start gap-2 rounded-control border border-warning/30 bg-warning/10 px-3 py-2 text-xs leading-relaxed text-warning">
            <Icon name="alertTriangle" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>
              If you run a cookie cleaner such as Cookie AutoDelete, allow-list
              myfigurecollection.net first. Otherwise it removes the session on the browser side
              and the value pasted here goes stale within hours.
            </span>
          </p>

          <p className="text-xs leading-relaxed text-faint">
            Careful which row you take: <span className="font-mono">addtl_consent</span>,{' '}
            <span className="font-mono">euconsent-v2</span> and{' '}
            <span className="font-mono">rzr_seg</span> sit right next to it and are cookie-banner
            leftovers, not the sign-in.
          </p>

          <Field
            label="PHPSESSID"
            hint="Stored on the server and never sent back to this page. Signing out on MyFigureCollection revokes it."
          >
            <input
              type="password"
              value={cookie}
              onChange={(e) => setCookie(e.target.value)}
              placeholder="e.g. a1b2c3d4e5f6..."
              className="field font-mono text-xs"
              autoComplete="off"
            />
          </Field>

          <p className="flex items-start gap-2 rounded-control border border-line bg-raised px-3 py-2 text-xs leading-relaxed text-faint">
            <Icon name="info" className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            A session cookie is asked for rather than your password so the password never reaches
            this database. Note that requests then carry your identity, so the deliberately slow
            rate matters more, not less.
          </p>

          <div className="flex gap-2">
            <button
              onClick={() => save.mutate()}
              disabled={save.isPending || !cookie.trim()}
              className="btn-primary text-sm"
            >
              {save.isPending && <Spinner className="h-3.5 w-3.5" />}
              Save session
            </button>
            {state.configured && (
              <button
                onClick={() => {
                  setCookie('')
                  save.mutate()
                }}
                className="btn-ghost text-sm text-danger"
              >
                Clear
              </button>
            )}
          </div>
        </div>
      )}

      {state.valid && (
        <button
          onClick={() => recheck.mutate()}
          disabled={recheck.isPending}
          className="btn-ghost mt-3 text-xs"
        >
          {recheck.isPending ? <Spinner className="h-3 w-3" /> : <Icon name="refresh" className="h-3 w-3" />}
          Re-read entries that were withheld earlier
        </button>
      )}
    </Card>
  )
}

/**
 * One shop slice.
 *
 * Two numbers that were previously conflated are now kept apart, because
 * mixing them produced the reading "52,691 of ~10,475": how much of the slice
 * this instance holds, counted from the database, and how far the current
 * pass has got through its pages. The cumulative listings-checked figure grows
 * by fifty per page on every pass forever and is labelled as such.
 */
function Slice({
  slice,
  onToggle,
  onRestart,
}: {
  slice: any
  onToggle: (enabled: boolean) => void
  onRestart: () => void
}) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)

  const save = useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      api.admin.updateCatalogSlice(slice.scope, body),
    onSuccess: () => {
      toast.success('Slice settings saved')
      void queryClient.invalidateQueries({ queryKey: ['admin', 'catalog'] })
    },
  })

  const resting = slice.next_run_in_seconds > 0
  // Mid-pass means the cursor has left page one, whatever the state says: a
  // slice that ran out of time reads "paused" but is still part way through,
  // and its bar should show that rather than jumping back to coverage.
  const sweeping = !resting && slice.cursor_page > 1

  return (
    <div>
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium">{slice.label}</span>
        <Badge
          tone={
            slice.state === 'running'
              ? 'accent'
              : slice.state === 'completed'
                ? 'positive'
                : slice.state === 'failed'
                  ? 'danger'
                  : 'neutral'
          }
        >
          {!slice.enabled ? 'off' : resting ? 'resting' : slice.state}
        </Badge>
        {!slice.enabled && (
          <span
            className={clsx(
              'text-[11px]',
              slice.scope === 'figures_in_stock' ? 'text-warning' : 'text-faint',
            )}
          >
            {slice.scope === 'figures_in_stock'
              ? 'off — nothing else notices a restock quickly'
              : slice.scope === 'figures_preowned'
                ? 'off — used listings only found by the weekly sweep'
                : 'off — the weekly sweep still finds these'}
          </span>
        )}
        {slice.first_pass_done && <Badge tone="positive">Swept at least once</Badge>}
        {/* Where it sits in the rotation. Slices take turns rather than
            running by rank, so "third in line" is the honest answer to why
            one has not moved for a while. */}
        {slice.state !== 'running' && slice.queue_position !== null && (
          <span className="text-[11px] text-faint">
            {slice.queue_position === 0 ? 'up next' : `${slice.queue_position + 1}. in line`}
          </span>
        )}
        <span className="ml-auto text-xs tabular-nums text-muted">
          {slice.total_results
            ? `${slice.items_local.toLocaleString('en-GB')} here · shop lists ~${slice.total_results.toLocaleString('en-GB')}`
            : 'not started'}
        </span>
      </div>

      {/* How far through the current pass, which is what a progress bar means
          to anyone looking at one. It used to show catalogue coverage, so it
          sat full and green while a fresh sweep was on page 3 of 211 - the
          one moment it should have been near empty. Coverage is still here,
          as the number underneath, where a figure that barely moves belongs. */}
      <Bar
        percent={sweeping ? slice.pass_percent : slice.coverage_percent}
        tone={sweeping ? 'accent' : slice.coverage_percent >= 95 ? 'positive' : 'accent'}
      />

      <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-faint">
        <span className="font-medium text-muted">
          {sweeping
            ? `${slice.pass_percent}% through this sweep`
            : `${slice.coverage_percent}% of what the shop lists`}
        </span>
        {sweeping && (
          <span>{slice.coverage_percent}% of the shop held</span>
        )}
        {slice.pages_this_cycle ? (
          <span>
            {slice.state === 'running' ? 'reading' : 'paused at'} page{' '}
            {slice.cursor_page.toLocaleString('en-GB')} of{' '}
            {slice.pages_this_cycle.toLocaleString('en-GB')} ({slice.pass_percent}%)
          </span>
        ) : null}
        {slice.items_changed > 0 && (
          <span>{slice.items_changed.toLocaleString('en-GB')} price changes seen</span>
        )}
        {resting ? (
          <span>next sweep in {duration(slice.next_run_in_seconds)}</span>
        ) : slice.eta_seconds ? (
          <span
            title={
              slice.pages_per_hour
                ? `Based on the ${slice.pages_per_hour} pages an hour this slice actually manages, waiting for its turn included.`
                : 'A rough guess until this slice has run once and its real speed is known.'
            }
          >
            this sweep done in {eta(slice.eta_seconds)}
            {!slice.pages_per_hour && ' (rough)'}
          </span>
        ) : null}
        {slice.last_run_at && <span>last read {relativeTime(slice.last_run_at)}</span>}
        {/* Rows this slice holds that the shop no longer counts in it. On the
            pre-owned slice that is not a fault, it is the whole point: a used
            listing is deleted when it sells and this is the only place it
            still exists. Elsewhere it means a status nothing has rechecked
            yet, which the next sweep corrects. */}
        {slice.stale_local > 0 && slice.total_results > 0 && (
          slice.scope === 'figures_preowned' ? (
            <span
              className="text-positive"
              title="A used listing is deleted the moment it sells. These are kept here with their prices and photos."
            >
              {slice.stale_local.toLocaleString('en-GB')} the shop has removed, kept here
            </span>
          ) : (
            <span className="text-warning" title="The next sweep corrects these.">
              {slice.stale_local.toLocaleString('en-GB')} awaiting a re-check
            </span>
          )
        )}

        <span className="ml-auto flex items-center gap-2">
          <button onClick={() => setOpen((v) => !v)} className="hover:text-ink">
            settings
          </button>
          <button onClick={onRestart} className="hover:text-ink" title="Rewind to page 1">
            rewind
          </button>
          <Toggle checked={slice.enabled} onChange={onToggle} label={slice.enabled ? 'on' : 'off'} />
        </span>
      </div>

      {open && (
        <div className="mt-2 grid gap-3 rounded-control border border-line bg-raised p-3 sm:grid-cols-3">
          <Field
            label="Re-check every"
            hint="How long to wait before reading the newest pages again."
          >
            <Interval
              minutes={slice.recheck_minutes}
              onChange={(minutes) => save.mutate({ recheck_interval_minutes: minutes })}
            />
          </Field>
          <p className="text-[11px] leading-relaxed text-faint sm:col-span-3">
            Every pass reads the whole slice. There used to be a shallow
            re-check of the first few pages between full sweeps, on the
            assumption that the shop lists newest first — measured against the
            live API, it does not. The pre-owned order is stable and unrelated
            to when a listing was added, so the shallow pass re-read the same
            products for ever and never saw the rest.
          </p>
          <RunLog runs={slice.recent_runs} />
          <p className="text-[11px] leading-relaxed text-faint sm:col-span-3">
            {slice.cycles_completed.toLocaleString('en-GB')} pass(es) so far,{' '}
            {slice.listings_checked.toLocaleString('en-GB')} listings checked in total. That
            counter accumulates across every pass, so it climbs well past the number of items
            the slice actually contains.
          </p>
        </div>
      )}

      {slice.last_error && <p className="mt-1 text-[11px] text-danger">{slice.last_error}</p>}
    </div>
  )
}

/**
 * An interval in whichever unit reads naturally.
 *
 * Stored in minutes throughout, because that is what the scheduler compares
 * against. Offered in days as well, since "every 14 days" is a schedule
 * someone can picture and "every 20,160 minutes" is a number to be counted on
 * fingers.
 */
function Interval({
  minutes,
  onChange,
}: {
  minutes: number
  onChange: (minutes: number) => void
}) {
  const [unit, setUnit] = useState<'minutes' | 'hours' | 'days'>(
    minutes % 1440 === 0 && minutes >= 1440
      ? 'days'
      : minutes % 60 === 0 && minutes >= 120
        ? 'hours'
        : 'minutes',
  )
  const per = unit === 'days' ? 1440 : unit === 'hours' ? 60 : 1
  const [value, setValue] = useState(String(minutes / per))

  function commit() {
    const total = Math.round(Number(value) * per)
    if (!Number.isFinite(total) || total <= 0) return
    // The same bounds the server enforces, so a rejected value never looks
    // accepted here first.
    const clamped = Math.max(5, Math.min(total, 1440 * 90))
    setValue(String(clamped / per))
    if (clamped !== minutes) onChange(clamped)
  }

  return (
    <div className="flex items-center gap-2">
      <input
        type="number"
        min={unit === 'minutes' ? 5 : 1}
        step={unit === 'minutes' ? 5 : 1}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onBlur={commit}
        className="field w-24 tabular-nums"
      />
      <select
        value={unit}
        onChange={(e) => {
          // Keep the schedule, change only how it is written.
          const next = e.target.value as 'minutes' | 'hours' | 'days'
          const shown = minutes / (next === 'days' ? 1440 : next === 'hours' ? 60 : 1)
          setUnit(next)
          setValue(String(Number(shown.toFixed(2))))
        }}
        className="field w-24 text-xs"
      >
        <option value="minutes">minutes</option>
        <option value="hours">hours</option>
        <option value="days">days</option>
      </select>
    </div>
  )
}

type Run = {
  at: string
  seconds: number
  pages: number
  items: number
  new: number
  changed: number
  stopped: string | null
  pages_per_minute: number | null
  errors: number
}

/**
 * What the last few runs of this slice actually managed.
 *
 * The pages-per-minute here is the speed while running, which is a different
 * question from the pages-per-hour behind the sweep estimate: that one
 * includes the hours spent waiting for a turn. A slice can be quick in this
 * sense and still take a day to get round, and seeing both is what stops that
 * looking like a contradiction.
 */
function RunLog({ runs }: { runs?: Run[] }) {
  if (!runs || runs.length === 0) {
    return (
      <p className="text-[11px] text-faint sm:col-span-3">
        No runs recorded yet. They appear here as the slice works.
      </p>
    )
  }

  return (
    <div className="sm:col-span-3">
      <p className="mb-1.5 text-[11px] font-medium text-muted">
        Last {runs.length} run{runs.length === 1 ? '' : 's'}, newest first
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-[11px] tabular-nums">
          <thead className="text-faint">
            <tr className="text-left">
              <th className="pb-1 pr-3 font-normal">Finished</th>
              <th className="pb-1 pr-3 font-normal">Ran for</th>
              <th className="pb-1 pr-3 text-right font-normal">Pages</th>
              <th className="pb-1 pr-3 text-right font-normal">Per min</th>
              <th className="pb-1 pr-3 text-right font-normal">New</th>
              <th className="pb-1 pr-3 text-right font-normal">Changed</th>
              <th className="pb-1 font-normal">Ended because</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.at} className="border-t border-line/60">
                <td className="py-1 pr-3 text-muted">
                  {new Date(run.at).toLocaleString('en-GB', {
                    day: '2-digit',
                    month: 'short',
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </td>
                <td className="py-1 pr-3 text-muted">{duration(run.seconds)}</td>
                <td className="py-1 pr-3 text-right">{run.pages.toLocaleString('en-GB')}</td>
                <td className="py-1 pr-3 text-right font-medium">
                  {run.pages_per_minute?.toLocaleString('en-GB') ?? '\u2014'}
                </td>
                <td className="py-1 pr-3 text-right">
                  {run.new > 0 ? (
                    <span className="text-positive">+{run.new.toLocaleString('en-GB')}</span>
                  ) : (
                    <span className="text-faint">0</span>
                  )}
                </td>
                <td className="py-1 pr-3 text-right">
                  {run.changed > 0 ? (
                    run.changed.toLocaleString('en-GB')
                  ) : (
                    <span className="text-faint">0</span>
                  )}
                </td>
                <td className={clsx('py-1', run.errors > 0 ? 'text-warning' : 'text-faint')}>
                  {run.stopped ?? 'finished the slice'}
                  {run.errors > 0 && ` \u00b7 ${run.errors} error(s)`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}


/**
 * How much of the pre-owned catalogue is being followed copy by copy.
 *
 * Two numbers matter and they mean different things. "Counter seen" is every
 * product we have opened at least once, which is enough for a turnover
 * estimate. "With an estimate" is every product that actually has a shelf-life
 * figure attached, by any of the three methods.
 */
function ShelfLifePanel() {
  const toast = useToast()
  const queryClient = useQueryClient()

  const shelf = useQuery({
    queryKey: ['admin', 'shelfLife'],
    queryFn: () => api.admin.shelfLife(),
    refetchInterval: 20_000,
  })

  const runNow = useMutation({
    mutationFn: () => api.admin.runShelfSampler(30),
    onSuccess: (result) => {
      toast.success('Sampler finished', result.message)
      void queryClient.invalidateQueries({ queryKey: ['admin', 'shelfLife'] })
    },
    onError: (error) => toast.error('Sampler failed', (error as Error).message),
  })

  const d = shelf.data?.detail as
    | {
        enabled: boolean
        preowned_total: number
        counter_seen: number
        with_estimate: number
        due_now: number
        tiers: Record<string, number>
        by_basis: Record<string, number>
        listings_total: number
        listings_live: number
        listings_departed: number
        requests_per_minute: number
      }
    | undefined

  if (!d) return null

  const pct = (n: number) => (d.preowned_total ? (n / d.preowned_total) * 100 : 0)
  const BASIS_LABEL: Record<string, string> = {
    observed: 'Watched copies sell',
    intake: 'Shop turnover',
    intake_bootstrap: 'Turnover, rough',
    product: 'Whole listing',
  }

  return (
    <Card className="p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <Icon name="clock" className="h-4 w-4 text-accent" />
            Shelf-life tracking
          </h3>
          <p className="mt-0.5 text-xs text-muted">
            Following individual pre-owned copies so a vanished listing becomes a recorded sale.
            {!d.enabled && ' Currently switched off.'}
          </p>
        </div>
        <button
          onClick={() => runNow.mutate()}
          disabled={runNow.isPending || !d.enabled}
          className="btn-quiet text-xs"
        >
          {runNow.isPending ? 'Sampling…' : 'Sample now'}
        </button>
      </div>

      <div className="space-y-3">
        <div>
          <div className="mb-1 flex justify-between text-xs">
            <span className="text-muted">Products opened at least once</span>
            <span className="tabular-nums">
              {d.counter_seen.toLocaleString('en-GB')} of{' '}
              {d.preowned_total.toLocaleString('en-GB')}
            </span>
          </div>
          <Bar percent={pct(d.counter_seen)} />
        </div>
        <div>
          <div className="mb-1 flex justify-between text-xs">
            <span className="text-muted">Products with a shelf-life figure</span>
            <span className="tabular-nums">{d.with_estimate.toLocaleString('en-GB')}</span>
          </div>
          <Bar percent={pct(d.with_estimate)} tone="positive" />
        </div>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3 border-t border-line pt-3 text-xs sm:grid-cols-4">
        <div>
          <p className="text-faint">Copies tracked</p>
          <p className="font-mono tabular-nums">{d.listings_total.toLocaleString('en-GB')}</p>
        </div>
        <div>
          <p className="text-faint">Still listed</p>
          <p className="font-mono tabular-nums">{d.listings_live.toLocaleString('en-GB')}</p>
        </div>
        <div>
          <p className="text-faint">Seen to sell</p>
          <p className="font-mono tabular-nums">{d.listings_departed.toLocaleString('en-GB')}</p>
        </div>
        <div>
          <p className="text-faint">Waiting for a look</p>
          <p className="font-mono tabular-nums">{d.due_now.toLocaleString('en-GB')}</p>
        </div>
      </div>

      {Object.keys(d.by_basis).length > 0 && (
        <div className="mt-3 border-t border-line pt-3">
          <p className="mb-1.5 text-xs text-faint">Where the figures come from</p>
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
            {Object.entries(d.by_basis).map(([basis, count]) => (
              <span key={basis} className="text-muted">
                {BASIS_LABEL[basis] ?? basis}:{' '}
                <span className="font-mono tabular-nums text-ink">
                  {count.toLocaleString('en-GB')}
                </span>
              </span>
            ))}
          </div>
        </div>
      )}

      {Object.keys(d.tiers).length > 0 && (
        <p className="mt-3 border-t border-line pt-2 text-xs text-faint">
          Attention split:{' '}
          {(['hot', 'warm', 'cold'] as const)
            .filter((t) => d.tiers[t])
            .map((t) => `${d.tiers[t].toLocaleString('en-GB')} ${t}`)
            .join(' · ')}{' '}
          · {d.requests_per_minute} requests a minute
        </p>
      )}
    </Card>
  )
}


/** Instance-wide behaviour, set once for everyone using this install. */
function BehaviourPanel() {
  const toast = useToast()
  const queryClient = useQueryClient()

  const behaviour = useQuery({
    queryKey: ['admin', 'behaviour'],
    queryFn: () => api.admin.behaviour(),
  })

  const save = useMutation({
    mutationFn: (body: Record<string, boolean>) => api.admin.setBehaviour(body),
    onSuccess: () => {
      toast.success('Saved', 'Applies to everyone using this instance.')
      void queryClient.invalidateQueries({ queryKey: ['admin', 'behaviour'] })
      void queryClient.invalidateQueries({ queryKey: ['config'] })
    },
    onError: (error) => toast.error('Could not save', (error as Error).message),
  })

  const flags = (behaviour.data?.detail as { refresh_on_open?: boolean } | undefined) ?? {}

  return (
    <Card className="p-4">
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        <Icon name="settings" className="h-4 w-4 text-accent" />
        Behaviour
      </h3>
      <p className="mb-3 mt-0.5 text-xs text-muted">
        How this instance behaves for everyone using it.
      </p>
      <Toggle
        checked={Boolean(flags.refresh_on_open)}
        onChange={(refresh_on_open) => save.mutate({ refresh_on_open })}
        label="Ask the shop for fresh data when an item is opened"
        hint="Prices and stock are always current when you look, at the cost of one upstream request per item viewed. That comes out of the same budget the crawler and your watches share, which is why it is set here rather than per person."
      />
    </Card>
  )
}


export function CatalogPanel() {
  const toast = useToast()
  const queryClient = useQueryClient()

  const catalog = useQuery({
    queryKey: ['admin', 'catalog'],
    queryFn: () => api.admin.catalog(),
    refetchInterval: 20_000,
  })

  const runNow = useMutation({
    mutationFn: () => api.admin.runCatalogCrawl(30),
    onSuccess: (result) => {
      toast.success('Crawl slice finished', result.message)
      void queryClient.invalidateQueries({ queryKey: ['admin', 'catalog'] })
    },
    onError: (error) => toast.error('Crawl failed', (error as Error).message),
  })

  const toggleSlice = useMutation({
    mutationFn: ({ scope, enabled }: { scope: string; enabled: boolean }) =>
      api.admin.updateCatalogSlice(scope, { enabled }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['admin', 'catalog'] }),
  })

  const restart = useMutation({
    mutationFn: (scope: string) => api.admin.updateCatalogSlice(scope, { restart: true }),
    onSuccess: () => {
      toast.success('Slice rewound to page 1')
      void queryClient.invalidateQueries({ queryKey: ['admin', 'catalog'] })
    },
  })

  const data = catalog.data?.detail
  const mfc = data?.mfc ?? {}
  const linkedPercent = data?.items_known ? (data.items_linked_to_mfc / data.items_known) * 100 : 0

  return (
    <section className="space-y-4">
      <SectionTitle
        title="Catalogue build"
        icon="box"
        subtitle="Shop listings are ingested in the background, then cross-referenced with MyFigureCollection"
        action={
          <button
            onClick={() => runNow.mutate()}
            disabled={runNow.isPending}
            className="btn-ghost text-sm"
          >
            {runNow.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Icon name="play" />}
            Crawl for 30s now
          </button>
        }
      />

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="p-4 lg:col-span-2">
          <div className="mb-3 flex items-center justify-between gap-3">
            <h3 className="text-sm font-semibold">Shop slices</h3>
            <span className="text-xs text-faint">
              {data?.requests_per_minute ?? '—'} req/min, irregularly spaced
            </span>
          </div>
          <p className="mb-1 max-w-3xl text-xs leading-relaxed text-muted">
            Four views of the same shop, read newest-updated first. Only one runs at a time:{' '}
            <strong className="text-ink">whichever is past its own interval</strong>, and it
            keeps going until that sweep is finished rather than handing over part way. The
            weekly full sweep fills whatever time the hourly ones leave, so it advances in
            long stretches and pauses when something falls due &mdash; its cursor is kept, so
            it resumes rather than restarts.
          </p>
          <p className="mb-3 max-w-3xl text-xs leading-relaxed text-faint">
            <strong className="text-muted">They overlap, and that is the point.</strong>{' '}
            &ldquo;All figures&rdquo; contains everything, used listings included. Subtract the
            other three and what remains is sold-out listings &mdash; every one of 150 sampled
            &mdash; which are frozen: the price cannot move and nothing can be bought. Such a
            listing only becomes interesting again by coming back into stock or by a used copy
            appearing, and those land in the in-stock and pre-owned slices. So the three narrow
            ones catch the changes between them, at about 315 pages a pass against the full
            sweep&rsquo;s 1,385. The full sweep is the backstop: it records a listing that
            appeared and sold out between two passes, and corrects anything nothing else
            revisited. Weekly is enough for that.
          </p>

          {catalog.isLoading ? (
            <Spinner className="h-4 w-4" />
          ) : (
            <div className="space-y-4">
              {(data?.slices ?? []).map((slice: any) => (
                <Slice
                  key={slice.scope}
                  slice={slice}
                  onToggle={(enabled) => toggleSlice.mutate({ scope: slice.scope, enabled })}
                  onRestart={() => restart.mutate(slice.scope)}
                />
              ))}
            </div>
          )}
        </Card>

        <Card className="p-4">
          <h3 className="mb-3 text-sm font-semibold">Cross-reference</h3>
          <p className="mb-1 flex items-baseline justify-between text-xs">
            <span className="text-muted">Linked to MyFigureCollection</span>
            <span className="font-medium tabular-nums">
              {(data?.items_linked_to_mfc ?? 0).toLocaleString('en-GB')} /{' '}
              {(data?.items_known ?? 0).toLocaleString('en-GB')}
            </span>
          </p>
          <Bar percent={linkedPercent} />

          <dl className="mt-4 space-y-1.5 text-xs">
            {[
              ['Still queued', (mfc.pending_items ?? 0).toLocaleString('en-GB')],
              ['Known tags', (mfc.tags ?? 0).toLocaleString('en-GB')],
              ['Rate', `${mfc.requests_per_minute ?? '—'} req/min`],
              ['Finishes in', eta(mfc.eta_seconds)],
            ].map(([label, value]) => (
              <div key={label as string} className="flex justify-between gap-3">
                <dt className="text-muted">{label}</dt>
                <dd className="tabular-nums">{value}</dd>
              </div>
            ))}
          </dl>

          <p className="mt-3 text-[11px] leading-relaxed text-faint">
            The first pass is the expensive one. Once the catalogue is built the shop only adds a
            modest number of listings a day, so the backlog stops growing.
          </p>
        </Card>
      </div>

      <LoadPanel />
      <ActivityPanel />
      <BehaviourPanel />
      <BackupPanel />
      <ShelfLifePanel />
      <ImageCache />
      <MfcSession />
    </section>
  )
}

function bytes(value: number | null | undefined): string {
  if (!value) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let n = value
  let i = 0
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024
    i += 1
  }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}

function ImageCache() {
  const toast = useToast()
  const queryClient = useQueryClient()

  const cache = useQuery({
    queryKey: ['admin', 'images'],
    queryFn: () => api.admin.images(),
    refetchInterval: 30_000,
  })

  const prefetch = useMutation({
    mutationFn: () => api.admin.prefetchImages(60),
    onSuccess: (result) => {
      toast.success(result.message)
      void queryClient.invalidateQueries({ queryKey: ['admin', 'images'] })
    },
  })
  const prune = useMutation({
    mutationFn: () => api.admin.pruneImages(),
    onSuccess: (result) => {
      toast.success(result.message)
      void queryClient.invalidateQueries({ queryKey: ['admin', 'images'] })
    },
  })

  const d = cache.data?.detail
  if (!d) return null

  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <Icon name="box" className="h-4 w-4 text-accent" />
            Product photos on disk
            {!d.enabled && <Badge tone="danger">Off</Badge>}
          </h3>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-muted">
            AmiAmi removes a pre-owned listing's pictures the moment it sells. Everything else
            about the figure survives here, so a local copy is what stops the record becoming a
            row with a broken frame.
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <button
            onClick={() => prefetch.mutate()}
            disabled={prefetch.isPending}
            className="btn-ghost text-xs"
          >
            {prefetch.isPending ? <Spinner className="h-3 w-3" /> : <Icon name="download" className="h-3 w-3" />}
            Cache 60 more
          </button>
          <button
            onClick={() => prune.mutate()}
            disabled={prune.isPending}
            className="btn-ghost text-xs"
          >
            <Icon name="trash" className="h-3 w-3" />
            Prune
          </button>
        </div>
      </div>

      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <div>
          <p className="mb-1 flex items-baseline justify-between text-xs">
            <span className="text-muted">Disk used</span>
            <span className="font-medium tabular-nums">
              {bytes(d.bytes)} of {bytes(d.budget_bytes)}
            </span>
          </p>
          <Bar percent={d.percent_of_budget} tone={d.percent_of_budget > 90 ? 'accent' : 'positive'} />
          <p className="mt-1.5 text-[11px] text-faint">
            {d.downloaded.toLocaleString('en-GB')} files, {bytes(d.average_bytes)} each on
            average
          </p>
        </div>

        <div>
          <p className="mb-1 flex items-baseline justify-between text-xs">
            <span className="text-muted">Downloaded</span>
            <span className="font-medium tabular-nums">
              {d.downloaded.toLocaleString('en-GB')} /{' '}
              {d.expected_images.toLocaleString('en-GB')}
            </span>
          </p>
          <Bar percent={d.coverage_percent} />
          <p className="mt-1.5 text-[11px] leading-relaxed text-faint">
            A photo is noted the moment the crawler sees it and fetched later, at{' '}
            {d.requests_per_minute}/min, so downloading does not compete with the crawl. That
            is why {d.count.toLocaleString('en-GB')} are known of while{' '}
            {d.downloaded.toLocaleString('en-GB')} are on disk, with{' '}
            {d.pending.toLocaleString('en-GB')} still queued.
          </p>
          <p className="mt-1.5 text-[11px] text-faint">
            {d.full_images ? 'Thumbnail and full image' : 'Thumbnails only'} per item; all of
            them would need about {bytes(d.projected_bytes)}
          </p>
        </div>
      </div>

      <dl className="mt-4 grid gap-x-6 gap-y-1.5 border-t border-line pt-3 text-xs sm:grid-cols-2">
        {Object.entries(d.by_kind ?? {}).map(([kind, info]: [string, any]) => (
          <div key={kind} className="flex justify-between gap-3">
            <dt className="text-muted">{kind === 'thumb' ? 'Thumbnails' : 'Full images'}</dt>
            <dd className="tabular-nums">
              {info.count.toLocaleString('en-GB')} · {bytes(info.bytes)}
            </dd>
          </div>
        ))}
        {d.gone_upstream > 0 && (
          <div className="flex justify-between gap-3">
            <dt className="text-muted">Gone from the shop</dt>
            <dd className="tabular-nums text-warning">{d.gone_upstream.toLocaleString('en-GB')}</dd>
          </div>
        )}
        <div className="flex justify-between gap-3">
          <dt className="text-muted">Stored in</dt>
          <dd className="truncate font-mono text-[11px]">{d.path}</dd>
        </div>
      </dl>
    </Card>
  )
}
