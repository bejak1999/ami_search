import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { Icon } from '@/components/Icon'
import { Badge, Card, SectionTitle, Spinner, Stat } from '@/components/ui'
import { dateTime, duration, relativeTime } from '@/lib/format'
import { useAuth } from '@/lib/auth'
import { useToast } from '@/lib/toast'

export function AdminPage() {
  const { user } = useAuth()
  const toast = useToast()
  const queryClient = useQueryClient()

  const status = useQuery({ queryKey: ['status'], queryFn: api.status, refetchInterval: 15_000 })
  const users = useQuery({ queryKey: ['admin', 'users'], queryFn: api.admin.users })
  const settings = useQuery({ queryKey: ['admin', 'settings'], queryFn: api.admin.settings })

  const updateUser = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) =>
      api.admin.updateUser(id, body),
    onSuccess: (result) => {
      toast.success(result.message)
      void queryClient.invalidateQueries({ queryKey: ['admin'] })
    },
    onError: (error) => toast.error('Could not update the user', (error as Error).message),
  })

  const vapid = useMutation({
    mutationFn: () => api.admin.generateVapid(),
    onSuccess: (result) => {
      const keys = result.detail as Record<string, string>
      void navigator.clipboard?.writeText(
        `VAPID_PUBLIC_KEY=${keys.VAPID_PUBLIC_KEY}\nVAPID_PRIVATE_KEY=${keys.VAPID_PRIVATE_KEY}`,
      )
      toast.success('VAPID keys copied to your clipboard', result.message)
    },
  })

  const scheduler = status.data?.scheduler
  const fx = status.data?.fx

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Administration</h1>
        <p className="mt-1 text-sm text-muted">
          Instance health, upstream shops and accounts. Version {status.data?.version ?? '—'}.
        </p>
      </header>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat
          label="Scheduler"
          value={scheduler?.running ? 'Running' : 'Stopped'}
          sub={`${scheduler?.inflight ?? 0} in flight · ${scheduler?.workers ?? 0} workers`}
          icon="clock"
          tone={scheduler?.running ? 'positive' : 'danger'}
        />
        <Stat
          label="Checks run"
          value={(scheduler?.runs_total ?? 0).toLocaleString()}
          sub={`${scheduler?.errors_total ?? 0} errors · ${scheduler?.alerts_total ?? 0} alerts`}
          icon="refresh"
        />
        <Stat
          label="Catalogue"
          value={(status.data?.items ?? 0).toLocaleString()}
          sub={`${(status.data?.price_points ?? 0).toLocaleString()} price observations`}
          icon="box"
        />
        <Stat
          label="Uptime"
          value={duration(status.data?.uptime_seconds)}
          sub={`${status.data?.users ?? 0} users · ${status.data?.watches ?? 0} watches`}
          icon="chart"
        />
      </div>

      <section>
        <SectionTitle title="Shops" icon="link" subtitle="Upstream health and rate limits" />
        <div className="grid gap-3 sm:grid-cols-2">
          {(status.data?.providers ?? []).map((provider) => (
            <Card key={provider.id} className="p-4">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="flex items-center gap-2 text-sm font-semibold">
                    {provider.name}
                    <Badge tone={provider.healthy ? 'positive' : 'danger'}>
                      {provider.healthy ? 'Healthy' : 'Circuit open'}
                    </Badge>
                  </p>
                  <p className="mt-1 text-xs leading-relaxed text-muted">{provider.description}</p>
                </div>
                <a
                  href={provider.home_url}
                  target="_blank"
                  rel="noreferrer"
                  className="btn-quiet shrink-0 p-1.5"
                >
                  <Icon name="external" className="h-3.5 w-3.5" />
                </a>
              </div>
              <div className="mt-3 grid grid-cols-3 gap-2 border-t border-line pt-3 text-xs">
                <div>
                  <p className="text-faint">Budget</p>
                  <p className="mt-0.5 font-medium tabular-nums">{provider.rate_per_minute}/min</p>
                </div>
                <div>
                  <p className="text-faint">Latency</p>
                  <p className="mt-0.5 font-medium tabular-nums">
                    {Math.round(provider.last_latency_ms)} ms
                  </p>
                </div>
                <div>
                  <p className="text-faint">Failures</p>
                  <p className="mt-0.5 font-medium tabular-nums">
                    {provider.circuit?.failures ?? 0}
                  </p>
                </div>
              </div>
            </Card>
          ))}
        </div>
      </section>

      <section>
        <SectionTitle
          title="Exchange rates"
          icon="yen"
          subtitle={
            fx?.age_seconds !== null && fx?.age_seconds !== undefined
              ? `Updated ${duration(fx.age_seconds)} ago from ${fx.source ?? 'cache'}`
              : 'No rates loaded yet'
          }
          action={
            <button
              onClick={() =>
                void api.refreshFx().then((result) => {
                  toast.success(result.message)
                  void queryClient.invalidateQueries({ queryKey: ['status'] })
                })
              }
              className="btn-ghost text-sm"
            >
              <Icon name="refresh" />
              Refresh
            </button>
          }
        />
        <Card className="scroll-x p-4">
          <div className="flex gap-4">
            {Object.entries(fx?.rates ?? {}).map(([code, rate]) => (
              <div key={code} className="shrink-0">
                <p className="text-xs text-faint">JPY → {code}</p>
                <p className="mt-0.5 font-mono text-sm tabular-nums">{rate.toFixed(6)}</p>
              </div>
            ))}
          </div>
          {fx?.stale && (
            <p className="mt-3 flex items-center gap-1.5 text-xs text-warning">
              <Icon name="alertTriangle" className="h-3.5 w-3.5" />
              These rates are stale. Landed prices may be slightly off.
            </p>
          )}
        </Card>
      </section>

      <section>
        <SectionTitle title="Accounts" icon="user" subtitle={`${users.data?.length ?? 0} registered`} />
        <Card className="scroll-x">
          <table className="w-full min-w-[640px] text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-faint">
                <th className="px-4 py-2.5 font-medium">User</th>
                <th className="px-4 py-2.5 font-medium">Role</th>
                <th className="px-4 py-2.5 font-medium text-right">Watches</th>
                <th className="px-4 py-2.5 font-medium text-right">Channels</th>
                <th className="px-4 py-2.5 font-medium text-right">Alerts</th>
                <th className="px-4 py-2.5 font-medium">Last seen</th>
                <th className="px-4 py-2.5" />
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {(users.data ?? []).map((entry: any) => (
                <tr key={entry.id} className={entry.is_active ? '' : 'opacity-50'}>
                  <td className="px-4 py-3">
                    <p className="font-medium">{entry.username}</p>
                    <p className="text-xs text-faint">{entry.email}</p>
                  </td>
                  <td className="px-4 py-3">
                    <select
                      value={entry.role}
                      disabled={entry.id === user?.id}
                      onChange={(e) =>
                        updateUser.mutate({ id: entry.id, body: { role: e.target.value } })
                      }
                      className="field py-1 text-xs"
                    >
                      <option value="user">user</option>
                      <option value="admin">admin</option>
                    </select>
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums">{entry.watch_count}</td>
                  <td className="px-4 py-3 text-right tabular-nums">{entry.channel_count}</td>
                  <td className="px-4 py-3 text-right tabular-nums">{entry.alert_count}</td>
                  <td className="px-4 py-3 text-xs text-muted" title={dateTime(entry.last_login_at)}>
                    {entry.last_login_at ? relativeTime(entry.last_login_at) : 'never'}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() =>
                        updateUser.mutate({
                          id: entry.id,
                          body: { is_active: !entry.is_active },
                        })
                      }
                      disabled={entry.id === user?.id}
                      className="btn-quiet px-2 py-1 text-xs disabled:opacity-30"
                    >
                      {entry.is_active ? 'Disable' : 'Enable'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      </section>

      <section>
        <SectionTitle
          title="Instance configuration"
          icon="settings"
          subtitle="Read-only. These come from environment variables."
        />
        <Card className="p-4">
          {!status.data?.webpush_configured && (
            <div className="mb-4 flex flex-wrap items-center gap-3 rounded-control border border-warning/40 bg-warning/10 p-3">
              <Icon name="alertTriangle" className="h-4 w-4 shrink-0 text-warning" />
              <p className="flex-1 text-sm">
                Browser push is unavailable because no VAPID keys are set.
              </p>
              <button
                onClick={() => vapid.mutate()}
                disabled={vapid.isPending}
                className="btn-ghost text-sm"
              >
                {vapid.isPending && <Spinner className="h-3.5 w-3.5" />}
                Generate and copy
              </button>
            </div>
          )}

          <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            {Object.entries(settings.data?.detail ?? {}).map(([key, value]) => (
              <div key={key} className="flex justify-between gap-4 border-b border-line/60 py-1.5">
                <dt className="text-muted">{key.replace(/_/g, ' ')}</dt>
                <dd className="truncate font-mono text-xs tabular-nums">
                  {typeof value === 'boolean' ? (value ? 'yes' : 'no') : String(value || '—')}
                </dd>
              </div>
            ))}
          </dl>
        </Card>
      </section>
    </div>
  )
}
