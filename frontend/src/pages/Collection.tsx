import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { CollectionStatus } from '@/api/types'
import { Icon } from '@/components/Icon'
import { Badge, Card, EmptyState, Field, Modal, SegmentedControl, Skeleton, Spinner, Stat } from '@/components/ui'
import { money, relativeTime, tidyName } from '@/lib/format'
import { useToast } from '@/lib/toast'
import clsx from 'clsx'

const STATUSES: { value: CollectionStatus | 'all'; label: string }[] = [
  { value: 'all', label: 'Everything' },
  { value: 'wishlist', label: 'Wishlist' },
  { value: 'ordered', label: 'Ordered' },
  { value: 'owned', label: 'Owned' },
  { value: 'sold', label: 'Sold' },
]

const PRIORITY_LABEL: Record<number, string> = { 1: 'Grail', 2: 'Normal', 3: 'Maybe' }

export function CollectionPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [status, setStatus] = useState<CollectionStatus | 'all'>('all')
  const [adding, setAdding] = useState(false)
  const [addInput, setAddInput] = useState('')
  const [addStatus, setAddStatus] = useState<CollectionStatus>('owned')

  const entries = useQuery({
    queryKey: ['collection', status],
    queryFn: () => api.collection.list(status === 'all' ? {} : { status }),
  })
  const summary = useQuery({ queryKey: ['collection', 'summary'], queryFn: api.collection.summary })

  const update = useMutation({
    mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) =>
      api.collection.update(id, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['collection'] }),
  })
  const remove = useMutation({
    mutationFn: (id: number) => api.collection.remove(id),
    onSuccess: () => {
      toast.success('Removed')
      void queryClient.invalidateQueries({ queryKey: ['collection'] })
    },
  })

  const add = useMutation({
    mutationFn: () =>
      api.collection.add({ item_code: addInput.trim(), status: addStatus }),
    onSuccess: (entry) => {
      toast.success(`Added ${entry.item.name.slice(0, 50)}`)
      setAdding(false)
      setAddInput('')
      void queryClient.invalidateQueries({ queryKey: ['collection'] })
    },
    onError: (error) => toast.error('Could not add that', (error as Error).message),
  })

  const detail = summary.data?.detail

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Collection</h1>
          <p className="mt-1 text-sm text-muted">
            What you want, what you ordered, and what is already on the shelf.
          </p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => setAdding(true)} className="btn-primary">
            <Icon name="plus" />
            Add an item
          </button>
          <a href="/api/collection/export?fmt=csv" className="btn-ghost" download>
            <Icon name="download" />
            Export CSV
          </a>
        </div>
      </header>

      {detail && (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <Stat
            label="Wishlist total"
            value={money(detail.wishlist_landed_total, detail.currency)}
            sub="Delivered, incl. import"
            icon="heart"
            tone="accent"
          />
          <Stat
            label="Spent"
            value={money(detail.spent, detail.currency)}
            sub={`${detail.counts?.owned ?? 0} owned items`}
            icon="yen"
          />
          <Stat
            label="Market value"
            value={money(detail.market_value, detail.currency)}
            sub="At today's shop prices"
            icon="chart"
          />
          <Stat
            label="Unrealised"
            value={money(detail.unrealized, detail.currency)}
            sub={detail.unrealized >= 0 ? 'Above what you paid' : 'Below what you paid'}
            icon={detail.unrealized >= 0 ? 'up' : 'down'}
            tone={detail.unrealized >= 0 ? 'positive' : 'danger'}
          />
        </div>
      )}

      <SegmentedControl value={status} onChange={setStatus} options={STATUSES} />

      {entries.isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, index) => (
            <Skeleton key={index} className="h-24 rounded-card" />
          ))}
        </div>
      ) : entries.data?.length ? (
        <div className="space-y-2">
          {entries.data.map((entry) => (
            <Card key={entry.id} hover className="flex items-start gap-4 p-3.5">
              <button
                onClick={() => entry.item.id && navigate(`/item/${entry.item.id}`)}
                className="h-20 w-20 shrink-0 overflow-hidden rounded-lg bg-raised"
              >
                {entry.item.image_url ? (
                  <img
                    src={entry.item.image_url}
                    alt=""
                    loading="lazy"
                    className="h-full w-full object-cover"
                  />
                ) : (
                  <span className="grid h-full place-items-center text-faint">
                    <Icon name="box" />
                  </span>
                )}
              </button>

              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge
                    tone={
                      entry.status === 'owned'
                        ? 'positive'
                        : entry.status === 'ordered'
                          ? 'info'
                          : entry.status === 'sold'
                            ? 'neutral'
                            : 'accent'
                    }
                  >
                    {entry.status}
                  </Badge>
                  {entry.priority === 1 && <Badge tone="warning">Grail</Badge>}
                  {entry.item.in_stock && <Badge tone="positive">In stock now</Badge>}
                  {entry.quantity > 1 && <Badge>×{entry.quantity}</Badge>}
                </div>

                <button
                  onClick={() => entry.item.id && navigate(`/item/${entry.item.id}`)}
                  className="mt-1 block w-full truncate text-left text-sm font-medium hover:text-accent"
                >
                  {tidyName(entry.item.name)}
                </button>

                <p className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
                  <span className="font-medium tabular-nums text-ink">
                    {money(entry.item.price, entry.item.currency)}
                  </span>
                  {entry.item.landed && (
                    <span className="tabular-nums">
                      {money(entry.item.landed.total, entry.item.landed.currency)} landed
                    </span>
                  )}
                  {entry.paid_price && (
                    <span className="tabular-nums">
                      paid {money(entry.paid_price, entry.paid_currency)}
                    </span>
                  )}
                  <span className="text-faint">updated {relativeTime(entry.updated_at)}</span>
                </p>

                {entry.notes && (
                  <p className="mt-1 line-clamp-2 text-xs italic text-faint">{entry.notes}</p>
                )}
              </div>

              <div className="flex shrink-0 flex-col gap-1.5">
                <select
                  value={entry.status}
                  onChange={(e) =>
                    update.mutate({ id: entry.id, body: { status: e.target.value } })
                  }
                  className="field py-1 text-xs"
                >
                  {STATUSES.filter((s) => s.value !== 'all').map((s) => (
                    <option key={s.value} value={s.value}>
                      {s.label}
                    </option>
                  ))}
                </select>
                <select
                  value={entry.priority}
                  onChange={(e) =>
                    update.mutate({ id: entry.id, body: { priority: Number(e.target.value) } })
                  }
                  className="field py-1 text-xs"
                >
                  {[1, 2, 3].map((value) => (
                    <option key={value} value={value}>
                      {PRIORITY_LABEL[value]}
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => remove.mutate(entry.id)}
                  className={clsx('btn-quiet justify-center py-1 text-xs text-danger')}
                >
                  <Icon name="trash" className="h-3 w-3" />
                  Remove
                </button>
              </div>
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState
          icon="heart"
          title="Nothing here yet"
          body="Add a figure by pasting its AmiAmi link below, or use the heart on any search result. Wishlist entries feed the deal radar, which tells you when something is unusually cheap against its own history."
          action={
            <button onClick={() => setAdding(true)} className="btn-primary">
              <Icon name="plus" />
              Add an item
            </button>
          }
        />
      )}

      <Modal
        open={adding}
        onClose={() => setAdding(false)}
        title="Add to your collection"
        subtitle="Paste an AmiAmi link or a product code."
        footer={
          <>
            <button onClick={() => setAdding(false)} className="btn-ghost">
              Cancel
            </button>
            <button
              onClick={() => add.mutate()}
              disabled={!addInput.trim() || add.isPending}
              className="btn-primary"
            >
              {add.isPending && <Spinner className="h-4 w-4" />}
              Add
            </button>
          </>
        }
      >
        <div className="space-y-4">
          <Field
            label="Link or product code"
            hint="The figure is looked up on the shop, so its price is tracked from now on."
          >
            <input
              value={addInput}
              onChange={(e) => setAddInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && addInput.trim()) add.mutate()
              }}
              placeholder="https://www.amiami.com/eng/detail/?gcode=FIGURE-153570-R"
              className="field"
              autoFocus
            />
          </Field>
          <Field label="Status">
            <SegmentedControl
              value={addStatus}
              onChange={setAddStatus}
              options={STATUSES.filter((s) => s.value !== 'all').map((s) => ({
                value: s.value as CollectionStatus,
                label: s.label,
              }))}
            />
          </Field>
        </div>
      </Modal>
    </div>
  )
}
