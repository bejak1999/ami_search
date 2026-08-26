import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { useToast } from '@/lib/toast'
import { duration, relativeTime } from '@/lib/format'
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

          {catalog.isLoading ? (
            <Spinner className="h-4 w-4" />
          ) : (
            <div className="space-y-4">
              {(data?.slices ?? []).map((slice: any) => (
                <div key={slice.scope}>
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
                      {slice.state}
                    </Badge>
                    {slice.first_pass_done && <Badge tone="positive">First pass done</Badge>}
                    {slice.cycles_completed > 0 && (
                      <span className="text-[11px] text-faint">
                        {slice.cycles_completed} pass(es)
                      </span>
                    )}
                    <span className="ml-auto text-xs tabular-nums text-muted">
                      {slice.total_results
                        ? `${slice.items_seen.toLocaleString()} of ~${slice.total_results.toLocaleString()}`
                        : 'not started'}
                    </span>
                  </div>

                  <Bar percent={slice.percent} tone={slice.first_pass_done ? 'positive' : 'accent'} />

                  <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-faint">
                    <span>
                      page {slice.cursor_page.toLocaleString()} of{' '}
                      {slice.pages_this_cycle ? slice.pages_this_cycle.toLocaleString() : '?'}
                    </span>
                    <span>{slice.items_new.toLocaleString()} new</span>
                    <span>{slice.items_changed.toLocaleString()} price changes</span>
                    {!slice.first_pass_done && slice.eta_seconds ? (
                      <span>finishes in {eta(slice.eta_seconds)}</span>
                    ) : null}
                    {slice.last_run_at && <span>ran {relativeTime(slice.last_run_at)}</span>}
                    <span className="ml-auto flex items-center gap-2">
                      <button
                        onClick={() => restart.mutate(slice.scope)}
                        className="hover:text-ink"
                        title="Rewind this slice to page 1"
                      >
                        rewind
                      </button>
                      <Toggle
                        checked={slice.enabled}
                        onChange={(enabled) => toggleSlice.mutate({ scope: slice.scope, enabled })}
                        label={slice.enabled ? 'on' : 'off'}
                      />
                    </span>
                  </div>

                  {slice.last_error && (
                    <p className="mt-1 text-[11px] text-danger">{slice.last_error}</p>
                  )}
                </div>
              ))}
            </div>
          )}
        </Card>

        <Card className="p-4">
          <h3 className="mb-3 text-sm font-semibold">Cross-reference</h3>
          <p className="mb-1 flex items-baseline justify-between text-xs">
            <span className="text-muted">Linked to MyFigureCollection</span>
            <span className="font-medium tabular-nums">
              {(data?.items_linked_to_mfc ?? 0).toLocaleString()} /{' '}
              {(data?.items_known ?? 0).toLocaleString()}
            </span>
          </p>
          <Bar percent={linkedPercent} />

          <dl className="mt-4 space-y-1.5 text-xs">
            {[
              ['Still queued', (mfc.pending_items ?? 0).toLocaleString()],
              ['Known tags', (mfc.tags ?? 0).toLocaleString()],
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
            {d.count.toLocaleString()} photos, {bytes(d.average_bytes)} each on average
          </p>
        </div>

        <div>
          <p className="mb-1 flex items-baseline justify-between text-xs">
            <span className="text-muted">Catalogue covered</span>
            <span className="font-medium tabular-nums">
              {d.count.toLocaleString()} / {d.expected_images.toLocaleString()}
            </span>
          </p>
          <Bar percent={d.coverage_percent} />
          <p className="mt-1.5 text-[11px] text-faint">
            {d.full_images ? 'Thumbnail and full image' : 'Thumbnails only'} per item; the whole
            catalogue would need about {bytes(d.projected_bytes)}
          </p>
        </div>
      </div>

      <dl className="mt-4 grid gap-x-6 gap-y-1.5 border-t border-line pt-3 text-xs sm:grid-cols-2">
        {Object.entries(d.by_kind ?? {}).map(([kind, info]: [string, any]) => (
          <div key={kind} className="flex justify-between gap-3">
            <dt className="text-muted">{kind === 'thumb' ? 'Thumbnails' : 'Full images'}</dt>
            <dd className="tabular-nums">
              {info.count.toLocaleString()} · {bytes(info.bytes)}
            </dd>
          </div>
        ))}
        {d.gone_upstream > 0 && (
          <div className="flex justify-between gap-3">
            <dt className="text-muted">Gone from the shop</dt>
            <dd className="tabular-nums text-warning">{d.gone_upstream.toLocaleString()}</dd>
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
