import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { Watch } from '@/api/types'
import { Icon } from '@/components/Icon'
import { WatchEditor } from '@/components/WatchEditor'
import { Badge, Card, EmptyState, Skeleton, Spinner, Tooltip } from '@/components/ui'
import { duration, money, relativeTime } from '@/lib/format'
import { useToast } from '@/lib/toast'
import clsx from 'clsx'

function WatchRow({
  watch,
  onEdit,
  onRefetch,
}: {
  watch: Watch
  onEdit: (watch: Watch) => void
  onRefetch: () => void
}) {
  const toast = useToast()
  const navigate = useNavigate()
  const [busy, setBusy] = useState(false)

  const toggle = useMutation({
    mutationFn: () => api.watches.update(watch.id, { enabled: !watch.enabled }),
    onSuccess: onRefetch,
  })
  const remove = useMutation({
    mutationFn: () => api.watches.remove(watch.id),
    onSuccess: () => {
      toast.success('Watch deleted')
      onRefetch()
    },
  })

  async function runNow() {
    setBusy(true)
    try {
      const result = await api.watches.run(watch.id)
      toast.success('Checked', result.message)
      onRefetch()
    } catch (error) {
      toast.error('Check failed', (error as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className={clsx('overflow-hidden', !watch.enabled && 'opacity-60')}>
      <div className="flex flex-wrap items-start gap-4 p-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={watch.kind === 'item' ? 'info' : 'accent'}>
              {watch.kind === 'item' ? 'Item' : 'Search'}
            </Badge>
            {watch.condition !== 'any' && (
              <Badge>{watch.condition === 'preowned' ? 'Pre-owned' : 'New'}</Badge>
            )}
            {watch.priority > 0 && <Badge tone="warning">Priority {watch.priority}</Badge>}
            {!watch.enabled && <Badge tone="danger">Paused</Badge>}
            {!watch.baselined && watch.enabled && (
              <Tooltip content="The first check records what already exists, so you are not flooded on day one.">
                <Badge tone="info">Baselining</Badge>
              </Tooltip>
            )}
          </div>

          <h3 className="mt-1.5 truncate text-[15px] font-semibold">
            {watch.label || watch.query || watch.item_code}
          </h3>

          <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
            {watch.target_price !== null && (
              <span className="inline-flex items-center gap-1">
                <Icon name="yen" className="h-3 w-3" />
                Target {money(watch.target_price, watch.target_currency)}
                <span className="text-faint">
                  ({watch.price_basis === 'landed' ? 'incl. import' : 'shop price'})
                </span>
              </span>
            )}
            <span className="inline-flex items-center gap-1">
              <Icon name="clock" className="h-3 w-3" />
              Every {duration(watch.effective_interval_seconds)}
              {watch.adaptive && <span className="text-faint">· adaptive</span>}
            </span>
            <span className="inline-flex items-center gap-1">
              <Icon name="bell" className="h-3 w-3" />
              {watch.alert_count} alerts
            </span>
            <span className="inline-flex items-center gap-1">
              <Icon name="box" className="h-3 w-3" />
              {watch.match_count} tracked
            </span>
          </p>

          {watch.last_error ? (
            <p className="mt-2 flex items-start gap-1.5 rounded-control bg-danger/10 px-2.5 py-1.5 text-xs text-danger">
              <Icon name="alertTriangle" className="mt-0.5 h-3.5 w-3.5" />
              {watch.last_error}
            </p>
          ) : (
            <p className="mt-1.5 text-[11px] text-faint">
              {watch.enabled
                ? `Last checked ${relativeTime(watch.last_run_at)} · next ${relativeTime(watch.next_run_at)}`
                : 'Paused'}
            </p>
          )}
        </div>

        <div className="flex shrink-0 gap-1.5">
          <button onClick={runNow} disabled={busy} className="btn-ghost px-2.5" title="Check now">
            {busy ? <Spinner /> : <Icon name="play" />}
          </button>
          <button
            onClick={() => toggle.mutate()}
            className="btn-ghost px-2.5"
            title={watch.enabled ? 'Pause' : 'Resume'}
          >
            <Icon name={watch.enabled ? 'pause' : 'play'} />
          </button>
          <button onClick={() => onEdit(watch)} className="btn-ghost px-2.5" title="Edit">
            <Icon name="edit" />
          </button>
          <button
            onClick={() => {
              if (confirm('Delete this watch and its alert history?')) remove.mutate()
            }}
            className="btn-ghost px-2.5 text-danger"
            title="Delete"
          >
            <Icon name="trash" />
          </button>
        </div>
      </div>

      {watch.recent_items.length > 0 && (
        <div className="scroll-x flex gap-2 border-t border-line bg-raised/50 px-4 py-3">
          {watch.recent_items.map((item) => (
            <button
              key={item.id ?? item.code}
              onClick={() => item.id && navigate(`/item/${item.id}`)}
              className="group relative h-16 w-16 shrink-0 overflow-hidden rounded-lg border border-line"
              title={`${item.name} — ${money(item.price, item.currency)}`}
            >
              {item.image_url ? (
                <img
                  src={item.image_url}
                  alt=""
                  loading="lazy"
                  className="h-full w-full object-cover transition-transform group-hover:scale-110"
                />
              ) : (
                <span className="grid h-full place-items-center text-faint">
                  <Icon name="box" />
                </span>
              )}
              {item.in_stock && (
                <span className="absolute bottom-0 left-0 right-0 bg-positive/90 py-0.5 text-center text-[9px] font-bold text-white">
                  IN STOCK
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </Card>
  )
}

export function WatchesPage() {
  const [params, setParams] = useSearchParams()
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<Watch | null>(null)
  const [creating, setCreating] = useState(params.get('new') === '1')

  useEffect(() => {
    if (params.get('new') === '1') {
      setCreating(true)
      setParams({}, { replace: true })
    }
  }, [params, setParams])

  const watches = useQuery({
    queryKey: ['watches'],
    queryFn: () => api.watches.list({ with_items: true }),
    refetchInterval: 30_000,
  })

  const refetch = () => {
    void queryClient.invalidateQueries({ queryKey: ['watches'] })
    void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
  }

  const active = watches.data?.filter((w) => w.enabled) ?? []
  const paused = watches.data?.filter((w) => !w.enabled) ?? []

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Watches</h1>
          <p className="mt-1 text-sm text-muted">
            Saved searches and tracked items. Each one polls on its own schedule.
          </p>
        </div>
        <button onClick={() => setCreating(true)} className="btn-primary">
          <Icon name="plus" />
          New watch
        </button>
      </header>

      {watches.isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 3 }).map((_, index) => (
            <Skeleton key={index} className="h-28 rounded-card" />
          ))}
        </div>
      ) : watches.data?.length ? (
        <div className="space-y-6">
          <div className="space-y-3">
            {active.map((watch) => (
              <WatchRow key={watch.id} watch={watch} onEdit={setEditing} onRefetch={refetch} />
            ))}
          </div>

          {paused.length > 0 && (
            <div>
              <h2 className="mb-3 text-sm font-semibold text-muted">Paused ({paused.length})</h2>
              <div className="space-y-3">
                {paused.map((watch) => (
                  <WatchRow key={watch.id} watch={watch} onEdit={setEditing} onRefetch={refetch} />
                ))}
              </div>
            </div>
          )}
        </div>
      ) : (
        <EmptyState
          icon="eye"
          title="No watches yet"
          body="A watch is a saved search plus a target price. When something crosses that price or comes back in stock, you get pushed straight away."
          action={
            <button onClick={() => setCreating(true)} className="btn-primary">
              <Icon name="plus" />
              Create your first watch
            </button>
          }
        />
      )}

      {(creating || editing) && (
        <WatchEditor
          open
          watch={editing}
          onClose={() => {
            setCreating(false)
            setEditing(null)
          }}
          onSaved={() => {
            setCreating(false)
            setEditing(null)
            refetch()
          }}
        />
      )}
    </div>
  )
}
