import { useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { Icon } from '@/components/Icon'
import { PriceChart } from '@/components/PriceChart'
import { WatchEditor } from '@/components/WatchEditor'
import { LandedTooltip } from '@/components/ItemCard'
import { Badge, Card, SegmentedControl, Skeleton, Spinner, Tooltip } from '@/components/ui'
import { dateTime, grams, money, percent, relativeTime, tidyName } from '@/lib/format'
import { useToast } from '@/lib/toast'
import clsx from 'clsx'

const RANGES = [
  { value: '90', label: '3M' },
  { value: '365', label: '1Y' },
  { value: '1095', label: '3Y' },
  { value: '3650', label: 'All' },
]

export function ItemDetailPage() {
  const { itemId } = useParams()
  const id = Number(itemId)
  const navigate = useNavigate()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [range, setRange] = useState('365')
  const [activeImage, setActiveImage] = useState(0)
  const [watchOpen, setWatchOpen] = useState(false)

  const history = useQuery({
    queryKey: ['item', id, 'history', range],
    queryFn: () => api.items.history(id, Number(range)),
    enabled: Number.isFinite(id),
  })
  const tags = useQuery({
    queryKey: ['item', id, 'tags'],
    queryFn: () => api.discover.itemTags(id),
    enabled: Number.isFinite(id),
  })

  const item = history.data?.item
  const stats = history.data?.stats

  const refresh = useMutation({
    mutationFn: () => api.items.refresh(id),
    onSuccess: () => {
      toast.success('Refreshed from the shop')
      void queryClient.invalidateQueries({ queryKey: ['item', id] })
    },
    onError: (error) => toast.error('Refresh failed', (error as Error).message),
  })

  const enrich = useMutation({
    mutationFn: () => api.discover.enrichItem(id, true),
    onSuccess: (result) => {
      if (result.ok) toast.success(result.message)
      else toast.error(result.message)
      void queryClient.invalidateQueries({ queryKey: ['item', id] })
    },
    onError: (error) => toast.error('Lookup failed', (error as Error).message),
  })

  const wishlist = useMutation({
    mutationFn: (status: 'wishlist' | 'owned') => api.collection.add({ item_id: id, status }),
    onSuccess: () => {
      toast.success('Saved to your collection')
      void queryClient.invalidateQueries({ queryKey: ['item', id] })
      void queryClient.invalidateQueries({ queryKey: ['collection'] })
    },
    onError: (error) => toast.error('Could not save', (error as Error).message),
  })

  if (history.isLoading) {
    return (
      <div className="grid gap-6 lg:grid-cols-[minmax(0,420px)_1fr]">
        <Skeleton className="aspect-square rounded-card" />
        <div className="space-y-4">
          <Skeleton className="h-8 w-3/4 rounded" />
          <Skeleton className="h-5 w-1/3 rounded" />
          <Skeleton className="h-40 rounded-card" />
        </div>
      </div>
    )
  }

  if (!item) {
    return (
      <Card className="p-8 text-center">
        <p className="text-sm text-muted">That item is not in this instance.</p>
        <button onClick={() => navigate('/search')} className="btn-primary mt-4">
          Back to search
        </button>
      </Card>
    )
  }

  const images = item.images?.length ? item.images : item.image_url ? [item.image_url] : []
  const belowAverage =
    item.average_price && item.price ? (1 - item.price / item.average_price) * 100 : null

  return (
    <div className="space-y-6">
      <button onClick={() => navigate(-1)} className="btn-quiet -ml-2 text-sm">
        <Icon name="chevronLeft" />
        Back
      </button>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,420px)_1fr]">
        <div className="space-y-3">
          <Card className="overflow-hidden">
            <div className="aspect-square bg-raised">
              {images[activeImage] ? (
                <img
                  src={images[activeImage]}
                  alt={item.name}
                  className="h-full w-full object-contain"
                />
              ) : (
                <div className="grid h-full place-items-center text-faint">
                  <Icon name="box" className="h-10 w-10" />
                </div>
              )}
            </div>
          </Card>

          {images.length > 1 && (
            <div className="scroll-x flex gap-2 pb-1">
              {images.map((src, index) => (
                <button
                  key={src}
                  onClick={() => setActiveImage(index)}
                  className={clsx(
                    'h-16 w-16 shrink-0 overflow-hidden rounded-lg border-2 transition-all',
                    index === activeImage
                      ? 'border-accent'
                      : 'border-transparent opacity-60 hover:opacity-100',
                  )}
                >
                  <img src={src} alt="" loading="lazy" className="h-full w-full object-cover" />
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="min-w-0 space-y-5">
          <div>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              {item.condition === 'preowned' && <Badge tone="accent">Pre-owned</Badge>}
              {item.condition_grade && <Badge>{item.condition_grade}</Badge>}
              {item.in_stock ? (
                <Badge tone="positive">In stock</Badge>
              ) : item.is_preorder ? (
                <Badge tone="info">Pre-order</Badge>
              ) : (
                <Badge tone="danger">Not available</Badge>
              )}
              {item.scale && <Badge>{item.scale}</Badge>}
            </div>
            <h1 className="text-2xl font-semibold leading-tight tracking-tight text-balance">
              {tidyName(item.name)}
            </h1>
            {item.name_jp && <p className="mt-1 text-sm text-muted">{item.name_jp}</p>}
            <p className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-muted">
              {item.maker && <span>{item.maker}</span>}
              {item.series && <span className="text-faint">·</span>}
              {item.series && <span>{item.series}</span>}
              {item.release_date && <span className="text-faint">·</span>}
              {item.release_date && <span>{item.release_date}</span>}
              <span className="font-mono text-xs text-faint">{item.code}</span>
            </p>
          </div>

          <Card className="p-4">
            <div className="flex flex-wrap items-end justify-between gap-4">
              <div>
                <div className="flex items-baseline gap-3">
                  <span className="text-3xl font-semibold tabular-nums">
                    {money(item.price, item.currency)}
                  </span>
                  {item.list_price && item.discount_pct && item.discount_pct > 0 && (
                    <>
                      <span className="text-sm text-faint line-through tabular-nums">
                        {money(item.list_price, item.currency)}
                      </span>
                      <Badge tone="positive">-{Math.round(item.discount_pct)}%</Badge>
                    </>
                  )}
                </div>
                {item.landed && (
                  <Tooltip content={<LandedTooltip item={item} />}>
                    <p className="mt-1.5 inline-flex cursor-help items-center gap-1.5 text-sm text-muted">
                      <Icon name="box" className="h-3.5 w-3.5" />
                      <span className="font-medium tabular-nums text-ink">
                        {money(item.landed.total, item.landed.currency)}
                      </span>
                      delivered, incl. shipping and import ({grams(item.landed.weight_grams)} est.)
                    </p>
                  </Tooltip>
                )}
              </div>

              <div className="flex flex-wrap gap-2">
                <button onClick={() => setWatchOpen(true)} className="btn-primary">
                  <Icon name="bell" />
                  Track price
                </button>
                <button
                  onClick={() => wishlist.mutate('wishlist')}
                  className={clsx('btn-ghost', item.in_collection && 'border-accent/50 text-accent')}
                >
                  <Icon name="heart" />
                  {item.in_collection === 'wishlist' ? 'On wishlist' : 'Wishlist'}
                </button>
                <button
                  onClick={() => refresh.mutate()}
                  disabled={refresh.isPending}
                  className="btn-ghost px-2.5"
                  title="Check the shop right now"
                >
                  {refresh.isPending ? <Spinner /> : <Icon name="refresh" />}
                </button>
                {item.url && (
                  <a href={item.url} target="_blank" rel="noreferrer" className="btn-ghost px-2.5" title="Open on AmiAmi">
                    <Icon name="external" />
                  </a>
                )}
              </div>
            </div>

            <div className="mt-4 grid grid-cols-2 gap-3 border-t border-line pt-4 sm:grid-cols-4">
              {[
                { label: 'Lowest seen', value: money(item.lowest_price, item.currency) },
                { label: 'Average', value: money(item.average_price, item.currency) },
                { label: 'Highest seen', value: money(item.highest_price, item.currency) },
                {
                  label: 'Tracked since',
                  value: stats?.tracked_since ? relativeTime(stats.tracked_since) : '—',
                },
              ].map((entry) => (
                <div key={entry.label}>
                  <p className="text-[11px] uppercase tracking-wide text-faint">{entry.label}</p>
                  <p className="mt-0.5 text-sm font-medium tabular-nums">{entry.value}</p>
                </div>
              ))}
            </div>

            {belowAverage !== null && belowAverage > 8 && (
              <p className="mt-3 flex items-center gap-1.5 rounded-control bg-positive/10 px-3 py-2 text-sm text-positive">
                <Icon name="fire" className="h-4 w-4" />
                {percent(belowAverage)} below its tracked average.
              </p>
            )}
          </Card>

          <Card className="p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h2 className="flex items-center gap-2 text-sm font-semibold">
                <Icon name="chart" className="h-4 w-4 text-accent" />
                Price history
                <span className="font-normal text-faint">
                  ({stats?.points ?? 0} recorded changes)
                </span>
              </h2>
              <SegmentedControl size="sm" value={range} onChange={setRange} options={RANGES} />
            </div>
            <PriceChart
              points={history.data?.points ?? []}
              currency={item.currency}
              height={280}
            />
          </Card>

          <Card className="p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h2 className="flex items-center gap-2 text-sm font-semibold">
                <Icon name="tag" className="h-4 w-4 text-accent" />
                MyFigureCollection
              </h2>
              <div className="flex items-center gap-2">
                {item.mfc_url && (
                  <a
                    href={item.mfc_url}
                    target="_blank"
                    rel="noreferrer"
                    className="btn-quiet text-xs"
                  >
                    Open entry
                    <Icon name="external" className="h-3 w-3" />
                  </a>
                )}
                <button
                  onClick={() => enrich.mutate()}
                  disabled={enrich.isPending}
                  className="btn-ghost px-2.5 py-1 text-xs"
                >
                  {enrich.isPending ? <Spinner className="h-3 w-3" /> : <Icon name="refresh" className="h-3 w-3" />}
                  {item.mfc_id ? 'Re-check' : 'Look up'}
                </button>
              </div>
            </div>

            {item.mfc_id ? (
              <>
                <p className="mb-3 text-xs text-muted">
                  {item.mfc_matched_by === 'jan' ? (
                    <>
                      Matched exactly by barcode <span className="font-mono">{item.jan_code}</span>.
                    </>
                  ) : (
                    <>
                      Probable match from the title ({percent((item.mfc_confidence ?? 0) * 100)}{' '}
                      confidence). Check the entry before trusting the tags.
                    </>
                  )}
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {(tags.data ?? []).map((tag) => (
                    <button
                      key={`${tag.kind}-${tag.slug}`}
                      onClick={() => navigate(`/discover?tag=${encodeURIComponent(tag.slug)}`)}
                      className={clsx(
                        'chip transition-colors hover:border-accent/50 hover:text-accent',
                        tag.kind !== 'tag' && 'border-info/30 text-info',
                      )}
                      title={`Find more items tagged ${tag.name}`}
                    >
                      {tag.name}
                    </button>
                  ))}
                  {tags.data?.length === 0 && (
                    <p className="text-xs text-faint">No tags on the linked entry.</p>
                  )}
                </div>
              </>
            ) : (
              <p className="text-xs text-muted">
                Not linked yet. The background job works through the catalogue slowly to stay
                polite to MyFigureCollection, or you can look this one up now.
              </p>
            )}
          </Card>

          {(item.spec || item.remarks) && (
            <Card className="p-4">
              <h2 className="mb-2 text-sm font-semibold">Specification</h2>
              {item.spec && (
                <pre className="whitespace-pre-wrap font-sans text-sm leading-relaxed text-muted">
                  {item.spec}
                </pre>
              )}
              {item.remarks && (
                <p className="mt-3 border-t border-line pt-3 text-sm leading-relaxed text-muted">
                  {item.remarks}
                </p>
              )}
            </Card>
          )}

          <p className="text-xs text-faint">
            First seen {dateTime(item.first_seen_at)} · last checked {relativeTime(item.last_seen_at)}
          </p>
        </div>
      </div>

      {watchOpen && (
        <WatchEditor
          open
          seedItem={item}
          onClose={() => setWatchOpen(false)}
          onSaved={() => {
            setWatchOpen(false)
            toast.success('Watch created')
            void queryClient.invalidateQueries({ queryKey: ['watches'] })
          }}
        />
      )}
    </div>
  )
}
