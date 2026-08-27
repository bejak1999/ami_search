import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { Item } from '@/api/types'
import { Icon, type IconName } from '@/components/Icon'
import { ItemCard, ItemCardSkeleton } from '@/components/ItemCard'
import { EMPTY_TAGS, TagFilter, type TagSelection } from '@/components/TagFilter'
import { WatchEditor } from '@/components/WatchEditor'
import { Card, EmptyState, SegmentedControl, Spinner } from '@/components/ui'
import { useToast } from '@/lib/toast'
import { useWishlistToggle } from '@/lib/useWishlist'

/**
 * A horizontally scrolling row of suggestions.
 *
 * Rails rather than a grid because each answers a different question, and a
 * single grid would flatten "cheap right now" and "from series you follow"
 * into one undifferentiated wall.
 */
function Rail({
  rail,
  onOpen,
  onWatch,
  onWishlist,
  onExplore,
}: {
  rail: any
  onOpen: (item: Item) => void
  onWatch: (item: Item) => void
  onWishlist: (item: Item) => void
  onExplore: (params: Record<string, unknown>) => void
}) {
  const scroller = useRef<HTMLDivElement>(null)

  const nudge = (direction: 1 | -1) => {
    scroller.current?.scrollBy({ left: direction * 640, behavior: 'smooth' })
  }

  return (
    <section>
      <div className="mb-3 flex items-end justify-between gap-4">
        <div className="min-w-0">
          <h2 className="flex items-center gap-2 text-lg font-semibold tracking-tight">
            <Icon name={rail.icon as IconName} className="h-4.5 w-4.5 text-accent" />
            {rail.title}
          </h2>
          <p className="mt-0.5 text-sm text-muted">{rail.subtitle}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {rail.explore && (
            <button onClick={() => onExplore(rail.explore)} className="btn-quiet text-sm">
              See all
              <Icon name="chevronRight" className="h-3.5 w-3.5" />
            </button>
          )}
          <button onClick={() => nudge(-1)} className="btn-ghost px-2 py-1.5" aria-label="Scroll left">
            <Icon name="chevronLeft" className="h-3.5 w-3.5" />
          </button>
          <button onClick={() => nudge(1)} className="btn-ghost px-2 py-1.5" aria-label="Scroll right">
            <Icon name="chevronRight" className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <div ref={scroller} className="scroll-x no-scrollbar flex gap-4 pb-2">
        {rail.items.map((item: Item) => (
          <div key={item.id ?? item.code} className="w-[200px] shrink-0">
            <ItemCard
              item={item}
              compact
              onOpen={onOpen}
              onWatch={onWatch}
              onWishlist={onWishlist}
            />
          </div>
        ))}
      </div>
    </section>
  )
}

export function DiscoverPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const queryClient = useQueryClient()
  const wishlist = useWishlistToggle()

  const [mode, setMode] = useState<'feed' | 'tags'>('feed')
  const [tags, setTags] = useState<TagSelection>(EMPTY_TAGS)
  const [watchSeed, setWatchSeed] = useState<Partial<Item> | null>(null)

  const feed = useQuery({
    queryKey: ['discover', 'feed'],
    queryFn: () => api.discover.feed(),
    enabled: mode === 'feed',
    staleTime: 120_000,
  })

  const byTag = useQuery({
    queryKey: ['discover', 'byTag', tags],
    enabled: mode === 'tags' && (tags.include.length > 0 || tags.exclude.length > 0),
    queryFn: () =>
      api.search.local({
        tags: tags.include,
        tag_mode: tags.mode,
        exclude_tags: tags.exclude,
        availability: 'buyable',
        per_page: 60,
        sort: 'newest',
      }),
  })

  const stats = useQuery({ queryKey: ['discover', 'stats'], queryFn: api.discover.stats })

  const enrichNow = useMutation({
    mutationFn: () => api.discover.runEnrichment(20),
    onSuccess: (result) => {
      toast.success('Cross-reference run finished', result.message)
      void queryClient.invalidateQueries({ queryKey: ['discover'] })
      void queryClient.invalidateQueries({ queryKey: ['localTags'] })
    },
  })

  /** Hand a rail's defining filter over to the search page. */
  function explore(params: Record<string, unknown>) {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null) search.set(key, String(value))
    }
    navigate(`/search?${search.toString()}`)
  }

  const rails = feed.data?.detail?.rails ?? []
  const linked = stats.data?.detail?.linked_items ?? 0

  const open = (item: Item) => item.id && navigate(`/item/${item.id}`)

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Discover</h1>
          <p className="mt-1 max-w-2xl text-sm text-muted">
            {mode === 'feed'
              ? 'Drawn from the catalogue this instance has built, and from what you already watch and want.'
              : 'Browse by the tags MyFigureCollection gives figures: character, series, pose, outfit, anything.'}
          </p>
        </div>
        <SegmentedControl
          value={mode}
          onChange={setMode}
          options={[
            { value: 'feed', label: 'For you', icon: 'sparkle' },
            { value: 'tags', label: 'By tag', icon: 'tag' },
          ]}
        />
      </header>

      {mode === 'feed' ? (
        feed.isLoading ? (
          <div className="space-y-8">
            {Array.from({ length: 2 }).map((_, r) => (
              <div key={r}>
                <div className="skeleton mb-3 h-6 w-56 rounded" />
                <div className="flex gap-4 overflow-hidden">
                  {Array.from({ length: 6 }).map((_, i) => (
                    <div key={i} className="w-[200px] shrink-0">
                      <ItemCardSkeleton />
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : rails.length ? (
          <div className="space-y-9">
            {rails.map((rail: any) => (
              <Rail
                key={rail.key}
                rail={rail}
                onOpen={open}
                onWatch={setWatchSeed}
                onWishlist={(item) => wishlist.mutate(item)}
                onExplore={explore}
              />
            ))}
          </div>
        ) : (
          <EmptyState
            icon="compass"
            title="Nothing to suggest yet"
            body="The catalogue is still being built. Once the crawler has some listings, this page fills itself; add a watch or a wishlist entry and it starts suggesting things like them."
            action={
              <button onClick={() => navigate('/search')} className="btn-primary">
                <Icon name="search" />
                Browse the catalogue
              </button>
            }
          />
        )
      ) : (
        <div className="grid gap-6 lg:grid-cols-[320px_1fr]">
          <aside className="space-y-4">
            <Card className="p-4">
              <TagFilter value={tags} onChange={setTags} />
            </Card>

            <Card className="space-y-3 p-3.5">
              <p className="text-xs font-semibold uppercase tracking-wide text-muted">
                Cross-reference
              </p>
              <div className="space-y-1 text-xs text-muted">
                {[
                  ['Linked items', linked],
                  ['Still queued', stats.data?.detail?.pending_items],
                  ['Known tags', stats.data?.detail?.tags],
                ].map(([label, value]) => (
                  <div key={label as string} className="flex justify-between">
                    <span>{label as string}</span>
                    <span className="font-medium tabular-nums text-ink">
                      {typeof value === 'number' ? value.toLocaleString('en-GB') : '—'}
                    </span>
                  </div>
                ))}
              </div>
              <button
                onClick={() => enrichNow.mutate()}
                disabled={enrichNow.isPending}
                className="btn-ghost w-full text-xs"
              >
                {enrichNow.isPending ? (
                  <Spinner className="h-3 w-3" />
                ) : (
                  <Icon name="refresh" className="h-3 w-3" />
                )}
                Cross-reference 20 more now
              </button>
              <p className="text-[11px] leading-relaxed text-faint">
                Runs in the background at a deliberately slow rate, so
                MyFigureCollection never sees a burst from your instance.
              </p>
            </Card>
          </aside>

          <section className="min-w-0 space-y-4">
            {tags.include.length === 0 && tags.exclude.length === 0 ? (
              <EmptyState
                icon="tag"
                title="Pick a tag to start"
                body="Click a tag to require it, click again to exclude it. Combining a character with a pose or an outfit is where this gets interesting, and excluding is how you say 'this series, but no nendoroids'."
              />
            ) : byTag.isLoading ? (
              <div className="grid-cards">
                {Array.from({ length: 8 }).map((_, i) => (
                  <ItemCardSkeleton key={i} />
                ))}
              </div>
            ) : byTag.data?.items.length ? (
              <>
                <p className="text-sm text-muted">
                  <strong className="text-ink tabular-nums">
                    {byTag.data.total.toLocaleString('en-GB')}
                  </strong>{' '}
                  buyable {byTag.data.total === 1 ? 'item matches' : 'items match'}
                </p>
                <div className="grid-cards">
                  {byTag.data.items.map((item) => (
                    <ItemCard
                      key={item.id ?? item.code}
                      item={item}
                      onOpen={open}
                      onWatch={setWatchSeed}
                      onWishlist={(target) => wishlist.mutate(target)}
                    />
                  ))}
                </div>
              </>
            ) : (
              <EmptyState
                icon="tag"
                title="Nothing carries that combination"
                body="Only items already cross-referenced with MyFigureCollection can match, and that runs slowly in the background. Try a single tag, or switch the combination to 'any of them'."
              />
            )}
          </section>
        </div>
      )}

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
