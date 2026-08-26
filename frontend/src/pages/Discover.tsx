import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { Item, TagRef } from '@/api/types'
import { Icon } from '@/components/Icon'
import { ItemCard, ItemCardSkeleton } from '@/components/ItemCard'
import { WatchEditor } from '@/components/WatchEditor'
import { Badge, Card, EmptyState, SegmentedControl, Spinner, Toggle } from '@/components/ui'
import { useToast } from '@/lib/toast'
import clsx from 'clsx'

const KIND_LABELS: Record<string, string> = {
  tag: 'Tag',
  origin: 'Series',
  character: 'Character',
  company: 'Company',
  artist: 'Artist',
  material: 'Material',
  classification: 'Category',
}

function TagPicker({
  selected,
  onToggle,
}: {
  selected: string[]
  onToggle: (slug: string) => void
}) {
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState<string>('')

  const tags = useQuery({
    queryKey: ['tags', query, kind],
    queryFn: () => api.discover.tags({ q: query || undefined, kind: kind || undefined, limit: 60 }),
    staleTime: 60_000,
  })

  const grouped = useMemo(() => {
    const map = new Map<string, TagRef[]>()
    for (const tag of tags.data ?? []) {
      if (!map.has(tag.kind)) map.set(tag.kind, [])
      map.get(tag.kind)!.push(tag)
    }
    return [...map.entries()]
  }, [tags.data])

  return (
    <Card className="space-y-3 p-4">
      <div className="relative">
        <Icon
          name="search"
          className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-faint"
        />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter tags: sword, twintails, Good Smile…"
          className="field pl-9 text-sm"
        />
      </div>

      <div className="scroll-x flex gap-1.5 pb-1">
        <button
          onClick={() => setKind('')}
          className={clsx('chip shrink-0', !kind && 'chip-active')}
        >
          Everything
        </button>
        {Object.entries(KIND_LABELS).map(([value, label]) => (
          <button
            key={value}
            onClick={() => setKind(value)}
            className={clsx('chip shrink-0', kind === value && 'chip-active')}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="max-h-72 space-y-3 overflow-y-auto pr-1">
        {tags.isLoading ? (
          <p className="py-6 text-center text-sm text-faint">Loading tags…</p>
        ) : grouped.length === 0 ? (
          <p className="py-6 text-center text-sm text-faint">
            No tags yet. Tags arrive as items get cross-referenced with MyFigureCollection.
          </p>
        ) : (
          grouped.map(([groupKind, groupTags]) => (
            <div key={groupKind}>
              <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-faint">
                {KIND_LABELS[groupKind] ?? groupKind}
              </p>
              <div className="flex flex-wrap gap-1.5">
                {groupTags.map((tag) => (
                  <button
                    key={`${tag.kind}-${tag.slug}`}
                    onClick={() => onToggle(tag.slug)}
                    className={clsx('chip', selected.includes(tag.slug) && 'chip-active')}
                  >
                    {selected.includes(tag.slug) && <Icon name="check" className="h-3 w-3" />}
                    {tag.name}
                    {tag.usage_count ? (
                      <span className="text-faint">{tag.usage_count}</span>
                    ) : null}
                  </button>
                ))}
              </div>
            </div>
          ))
        )}
      </div>
    </Card>
  )
}

export function DiscoverPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [selected, setSelected] = useState<string[]>(() => params.getAll('tag'))
  const [source, setSource] = useState<'local' | 'mfc'>('local')
  const [inStockOnly, setInStockOnly] = useState(false)
  const [figuresOnly, setFiguresOnly] = useState(true)
  const [mfcPage, setMfcPage] = useState(1)
  const [watchSeed, setWatchSeed] = useState<Partial<Item> | null>(null)

  useEffect(() => {
    setParams(selected.length ? { tag: selected } : {}, { replace: true })
  }, [selected, setParams])

  const toggle = (slug: string) =>
    setSelected((prev) => (prev.includes(slug) ? prev.filter((s) => s !== slug) : [...prev, slug]))

  const local = useQuery({
    queryKey: ['discover', 'local', selected, inStockOnly],
    enabled: source === 'local',
    queryFn: () =>
      api.discover.local({
        tags: selected,
        in_stock: inStockOnly ? true : undefined,
        limit: 60,
      }),
  })

  const stats = useQuery({ queryKey: ['discover', 'stats'], queryFn: api.discover.stats })

  const viaMfc = useMutation({
    mutationFn: () =>
      api.discover.viaMfc({ tags: selected, page: mfcPage, figures_only: figuresOnly, lookups: 12 }),
    onError: (error) => toast.error('MyFigureCollection lookup failed', (error as Error).message),
  })

  const wishlist = useMutation({
    mutationFn: (item: Item) => api.collection.add({ item_id: item.id!, status: 'wishlist' }),
    onSuccess: () => {
      toast.success('Added to your wishlist')
      void queryClient.invalidateQueries({ queryKey: ['discover'] })
    },
  })

  const enrichNow = useMutation({
    mutationFn: () => api.discover.runEnrichment(20),
    onSuccess: (result) => {
      toast.success('Cross-reference run finished', result.message)
      void queryClient.invalidateQueries({ queryKey: ['discover'] })
      void queryClient.invalidateQueries({ queryKey: ['tags'] })
    },
  })

  const detail = viaMfc.data?.detail

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Discover</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted">
          Items are cross-referenced with MyFigureCollection by barcode, so you can browse AmiAmi by
          the tags MFC gives figures — character, series, pose, outfit, anything.
        </p>
      </header>

      <div className="grid gap-6 lg:grid-cols-[320px_1fr]">
        <aside className="space-y-4">
          <TagPicker selected={selected} onToggle={toggle} />

          {selected.length > 0 && (
            <Card className="space-y-2 p-3.5">
              <div className="flex items-center justify-between">
                <p className="text-xs font-semibold uppercase tracking-wide text-muted">
                  {selected.length} tag{selected.length === 1 ? '' : 's'} selected
                </p>
                <button onClick={() => setSelected([])} className="btn-quiet px-1.5 py-0.5 text-xs">
                  Clear
                </button>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {selected.map((slug) => (
                  <button key={slug} onClick={() => toggle(slug)} className="chip chip-active">
                    {slug.replace(/_/g, ' ').replace(/___$/, '')}
                    <Icon name="close" className="h-3 w-3" />
                  </button>
                ))}
              </div>
              <p className="text-[11px] leading-relaxed text-faint">
                Multiple tags are combined with AND.
              </p>
            </Card>
          )}

          <Card className="space-y-3 p-3.5">
            <p className="text-xs font-semibold uppercase tracking-wide text-muted">Cross-reference</p>
            <div className="space-y-1 text-xs text-muted">
              <div className="flex justify-between">
                <span>Linked items</span>
                <span className="font-medium tabular-nums text-ink">
                  {stats.data?.detail?.linked_items ?? '—'}
                </span>
              </div>
              <div className="flex justify-between">
                <span>Still queued</span>
                <span className="font-medium tabular-nums text-ink">
                  {stats.data?.detail?.pending_items ?? '—'}
                </span>
              </div>
              <div className="flex justify-between">
                <span>Known tags</span>
                <span className="font-medium tabular-nums text-ink">
                  {stats.data?.detail?.tags ?? '—'}
                </span>
              </div>
            </div>
            <button
              onClick={() => enrichNow.mutate()}
              disabled={enrichNow.isPending}
              className="btn-ghost w-full text-xs"
            >
              {enrichNow.isPending ? <Spinner className="h-3 w-3" /> : <Icon name="refresh" className="h-3 w-3" />}
              Cross-reference 20 more now
            </button>
            <p className="text-[11px] leading-relaxed text-faint">
              This runs automatically in the background at a deliberately slow rate, so
              MyFigureCollection never sees a burst from your instance.
            </p>
          </Card>
        </aside>

        <section className="min-w-0 space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <SegmentedControl
              value={source}
              onChange={setSource}
              options={[
                { value: 'local', label: 'Already on AmiAmi', icon: 'box' },
                { value: 'mfc', label: 'Search MyFigureCollection', icon: 'compass' },
              ]}
            />
            {source === 'local' ? (
              <Toggle checked={inStockOnly} onChange={setInStockOnly} label="In stock only" />
            ) : (
              <>
                <Toggle checked={figuresOnly} onChange={setFiguresOnly} label="Figures only" />
                <button
                  onClick={() => viaMfc.mutate()}
                  disabled={selected.length === 0 || viaMfc.isPending}
                  className="btn-primary ml-auto"
                >
                  {viaMfc.isPending ? <Spinner /> : <Icon name="search" />}
                  Search
                </button>
              </>
            )}
          </div>

          {source === 'local' ? (
            local.isLoading ? (
              <div className="grid-cards">
                {Array.from({ length: 8 }).map((_, index) => (
                  <ItemCardSkeleton key={index} />
                ))}
              </div>
            ) : local.data?.length ? (
              <>
                <p className="text-sm text-muted">
                  <strong className="text-ink tabular-nums">{local.data.length}</strong> item
                  {local.data.length === 1 ? '' : 's'} in this instance match
                  {selected.length ? ' every selected tag' : ''}.
                </p>
                <div className="grid-cards">
                  {local.data.map((item) => (
                    <ItemCard
                      key={item.id ?? item.code}
                      item={item}
                      onOpen={(target) => target.id && navigate(`/item/${target.id}`)}
                      onWatch={(target) => setWatchSeed(target)}
                      onWishlist={(target) => wishlist.mutate(target)}
                    />
                  ))}
                </div>
              </>
            ) : (
              <EmptyState
                icon="compass"
                title={selected.length ? 'Nothing here carries those tags yet' : 'Pick a tag to start'}
                body={
                  selected.length
                    ? 'Only items this instance has already seen and cross-referenced can match. Try the MyFigureCollection search instead, which reaches everything.'
                    : 'Choose one or more tags on the left. Combining a character with a pose or an outfit is where this gets interesting.'
                }
              />
            )
          ) : detail ? (
            <>
              <div className="flex flex-wrap items-center gap-3 text-sm text-muted">
                <span>{viaMfc.data?.message}</span>
                {detail.total_pages > 1 && (
                  <span className="ml-auto flex items-center gap-2">
                    <button
                      onClick={() => {
                        setMfcPage((p) => Math.max(1, p - 1))
                        setTimeout(() => viaMfc.mutate(), 0)
                      }}
                      disabled={mfcPage <= 1}
                      className="btn-ghost px-2 py-1"
                    >
                      <Icon name="chevronLeft" className="h-3.5 w-3.5" />
                    </button>
                    <span className="tabular-nums">
                      {detail.page} / {detail.total_pages}
                    </span>
                    <button
                      onClick={() => {
                        setMfcPage((p) => p + 1)
                        setTimeout(() => viaMfc.mutate(), 0)
                      }}
                      disabled={mfcPage >= detail.total_pages}
                      className="btn-ghost px-2 py-1"
                    >
                      <Icon name="chevronRight" className="h-3.5 w-3.5" />
                    </button>
                  </span>
                )}
              </div>

              <div className="grid-cards">
                {detail.results.map((row: any) =>
                  row.item ? (
                    <ItemCard
                      key={row.mfc_id}
                      item={row.item}
                      onOpen={(target) => target.id && navigate(`/item/${target.id}`)}
                      onWatch={(target) => setWatchSeed(target)}
                      onWishlist={(target) => wishlist.mutate(target)}
                    />
                  ) : (
                    <Card key={row.mfc_id} className="flex flex-col overflow-hidden opacity-70">
                      <div className="relative aspect-[3/4] bg-raised">
                        {row.mfc_image ? (
                          <img
                            src={row.mfc_image}
                            alt=""
                            loading="lazy"
                            className="h-full w-full object-cover grayscale"
                          />
                        ) : (
                          <div className="grid h-full place-items-center text-faint">
                            <Icon name="box" className="h-8 w-8" />
                          </div>
                        )}
                        <span className="absolute right-2 top-2">
                          <Badge tone={row.state === 'unmatched' ? 'danger' : 'neutral'}>
                            {row.state === 'unmatched' ? 'Not on AmiAmi' : 'Not checked yet'}
                          </Badge>
                        </span>
                      </div>
                      <div className="flex flex-1 flex-col gap-2 p-3">
                        <p className="line-clamp-3 text-xs leading-snug">{row.mfc_title}</p>
                        <a
                          href={row.mfc_url}
                          target="_blank"
                          rel="noreferrer"
                          className="btn-quiet mt-auto justify-start px-0 text-xs"
                        >
                          View on MFC
                          <Icon name="external" className="h-3 w-3" />
                        </a>
                      </div>
                    </Card>
                  ),
                )}
              </div>
              <p className="text-xs text-faint">
                Items marked <em>not checked yet</em> are looked up a few at a time to stay within
                the shop request budget. Run the search again to check the next batch.
              </p>
            </>
          ) : (
            <EmptyState
              icon="compass"
              title="Browse MyFigureCollection by tag"
              body="This asks MFC which figures carry your tags, then looks each one up on AmiAmi. Slower than the local index, but it reaches figures nobody here has searched for yet."
            />
          )}
        </section>
      </div>

      {watchSeed && (
        <WatchEditor
          open
          seedItem={watchSeed}
          onClose={() => setWatchSeed(null)}
          onSaved={() => {
            setWatchSeed(null)
            toast.success('Watch created')
            void queryClient.invalidateQueries({ queryKey: ['watches'] })
          }}
        />
      )}
    </div>
  )
}
