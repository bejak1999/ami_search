import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { api, ApiError } from '@/api/client'
import type { Item } from '@/api/types'
import { Icon } from '@/components/Icon'
import { ItemCard, ItemCardSkeleton } from '@/components/ItemCard'
import { EMPTY_TAGS, TagFilter, type TagSelection } from '@/components/TagFilter'
import { WatchEditor } from '@/components/WatchEditor'
import { Badge, Card, EmptyState, Field, SegmentedControl, Spinner, Toggle } from '@/components/ui'
import { useToast } from '@/lib/toast'
import { useWishlistToggle } from '@/lib/useWishlist'

type Source = 'local' | 'shop'

interface Filters {
  q: string
  condition: 'any' | 'new' | 'preowned'
  availability: 'any' | 'buyable' | 'in_stock' | 'preorder' | 'delisted'
  sort: string
  minPrice: string
  maxPrice: string
  atLowestEver: boolean
  exclude: string
  onSale: boolean
}

const EMPTY: Filters = {
  q: '',
  condition: 'any',
  availability: 'any',
  sort: 'newest',
  minPrice: '',
  maxPrice: '',
  atLowestEver: false,
  exclude: '',
  onSale: false,
}

/** Sorting the catalogue can do, some of which the shop cannot. */
const LOCAL_SORTS = [
  { value: 'newest', label: 'Newest here' },
  { value: 'price_asc', label: 'Cheapest' },
  { value: 'price_desc', label: 'Dearest' },
  { value: 'discount', label: 'Biggest discount' },
  { value: 'lowest_ever', label: 'Near its lowest ever' },
  { value: 'release', label: 'Release date' },
  { value: 'oldest', label: 'Oldest here' },
]

const SHOP_SORTS = [
  { value: 'newest', label: 'Newest' },
  { value: 'preowned', label: 'Pre-owned first' },
  { value: 'price_asc', label: 'Cheapest on page' },
  { value: 'price_desc', label: 'Dearest on page' },
  { value: 'discount', label: 'Biggest discount' },
  { value: 'release', label: 'Release date' },
]

export function SearchPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()

  const [source, setSource] = useState<Source>('local')
  const [draft, setDraft] = useState<Filters>({ ...EMPTY, q: params.get('q') ?? '' })
  const [applied, setApplied] = useState<Filters>({ ...EMPTY, q: params.get('q') ?? '' })
  const [tags, setTags] = useState<TagSelection>(EMPTY_TAGS)
  const [page, setPage] = useState(1)
  const [showFilters, setShowFilters] = useState(false)
  const [watchSeed, setWatchSeed] = useState<Partial<Item> | null>(null)

  const wishlist = useWishlistToggle()

  // A tag arriving from an item page should apply straight away.
  useEffect(() => {
    const incoming = params.getAll('tag')
    if (incoming.length) {
      setTags((prev) => ({ ...prev, include: incoming }))
      setSource('local')
    }
    const q = params.get('q')
    if (q !== null && q !== applied.q) {
      setDraft((d) => ({ ...d, q }))
      setApplied((a) => ({ ...a, q }))
    }
    // Only react to the URL changing from outside this component.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params])

  const summary = useQuery({
    queryKey: ['localSummary'],
    queryFn: () => api.search.localSummary(),
    staleTime: 60_000,
  })

  const results = useQuery({
    queryKey: ['search', source, applied, tags, page],
    // The catalogue view always has something to show, so it runs unprompted.
    // The shop is a live request, so it waits for something to ask for.
    enabled: source === 'local' || Boolean(applied.q.trim()),
    staleTime: 30_000,
    queryFn: () =>
      source === 'local'
        ? api.search.local({
            q: applied.q,
            page,
            per_page: 48,
            condition: applied.condition,
            availability: applied.availability,
            sort: applied.sort,
            tags: tags.include,
            tag_mode: tags.mode,
            exclude_tags: tags.exclude,
            min_price: applied.minPrice ? Number(applied.minPrice) : undefined,
            max_price: applied.maxPrice ? Number(applied.maxPrice) : undefined,
            at_lowest_ever: applied.atLowestEver,
          })
        : api.search.run({
            q: applied.q,
            page,
            per_page: 48,
            condition: applied.condition,
            stock: applied.availability === 'in_stock' ? 'in_stock' : 'any',
            sort: applied.sort === 'newest' ? 'newest' : applied.sort,
            min_price: applied.minPrice ? Number(applied.minPrice) : undefined,
            max_price: applied.maxPrice ? Number(applied.maxPrice) : undefined,
            exclude: applied.exclude
              ? applied.exclude.split(',').map((s) => s.trim()).filter(Boolean)
              : [],
            on_sale: applied.onSale,
          }),
  })

  const resolve = useMutation({
    mutationFn: (input: string) => api.search.resolve(input),
    onSuccess: (item) => item.id && navigate(`/item/${item.id}`),
    onError: (error) =>
      toast.error('Could not open that link', error instanceof ApiError ? error.message : undefined),
  })

  function submit(event?: React.FormEvent) {
    event?.preventDefault()
    const term = draft.q.trim()
    // A pasted product link is almost never meant as a search term.
    if (/amiami\.com|^[A-Z]{3,}-[A-Z0-9-]+$/i.test(term)) {
      resolve.mutate(term)
      return
    }
    setApplied(draft)
    setPage(1)
    setParams(term ? { q: term } : {}, { replace: true })
  }

  function switchSource(next: Source) {
    setSource(next)
    setPage(1)
    // "Newest" means different things either side, and the rest do not all
    // exist on both, so reset to the one sort both understand.
    setDraft((d) => ({ ...d, sort: 'newest' }))
    setApplied((a) => ({ ...a, sort: 'newest' }))
  }

  const catalogue = summary.data?.detail
  const activeFilters =
    (applied.condition !== 'any' ? 1 : 0) +
    (applied.availability !== 'any' ? 1 : 0) +
    (applied.minPrice ? 1 : 0) +
    (applied.maxPrice ? 1 : 0) +
    (applied.atLowestEver ? 1 : 0) +
    (applied.exclude ? 1 : 0) +
    (applied.onSale ? 1 : 0) +
    tags.include.length +
    tags.exclude.length

  const sorts = source === 'local' ? LOCAL_SORTS : SHOP_SORTS

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Search</h1>
          <p className="mt-1 text-sm text-muted">
            {source === 'local' ? (
              catalogue ? (
                <>
                  {catalogue.items.toLocaleString('en-GB')} figures tracked here
                  {catalogue.delisted > 0 && (
                    <>
                      , including{' '}
                      <strong className="text-ink">
                        {catalogue.delisted.toLocaleString('en-GB')}
                      </strong>{' '}
                      the shop has already removed
                    </>
                  )}
                  .
                </>
              ) : (
                'Everything this instance has ever seen.'
              )
            ) : (
              'Live from AmiAmi. Only what they will sell you today.'
            )}
          </p>
        </div>
        <SegmentedControl
          value={source}
          onChange={switchSource}
          options={[
            { value: 'local', label: 'Our catalogue', icon: 'box' },
            { value: 'shop', label: 'AmiAmi live', icon: 'link' },
          ]}
        />
      </header>

      <form onSubmit={submit} className="space-y-3">
        <div className="flex gap-2">
          <div className="relative flex-1">
            <Icon
              name="search"
              className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-faint"
            />
            <input
              value={draft.q}
              onChange={(e) => setDraft({ ...draft, q: e.target.value })}
              placeholder={
                source === 'local'
                  ? 'Filter the catalogue, or paste an amiami.com link'
                  : 'Search AmiAmi, or paste a product link'
              }
              className="field h-11 pl-10 text-[15px]"
            />
            {resolve.isPending && (
              <Spinner className="absolute right-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-accent" />
            )}
          </div>
          <button
            type="button"
            onClick={() => setShowFilters((v) => !v)}
            className={clsx('btn-ghost h-11 px-3.5', activeFilters > 0 && 'border-accent/50 text-accent')}
          >
            <Icon name="filter" />
            <span className="hidden sm:inline">Filters</span>
            {activeFilters > 0 && (
              <span className="rounded-full bg-accent px-1.5 text-[10px] font-bold text-accent-ink">
                {activeFilters}
              </span>
            )}
          </button>
          <button type="submit" className="btn-primary h-11 px-5">
            Search
          </button>
        </div>

        {showFilters && (
          <Card className="animate-fade-up p-4">
            <div className="grid gap-5 lg:grid-cols-[1fr_320px]">
              <div className="space-y-4">
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Condition">
                    <SegmentedControl
                      size="sm"
                      value={draft.condition}
                      onChange={(condition) => setDraft({ ...draft, condition })}
                      options={[
                        { value: 'any', label: 'Any' },
                        { value: 'new', label: 'New' },
                        { value: 'preowned', label: 'Pre-owned' },
                      ]}
                    />
                  </Field>
                  <Field label="Sort by">
                    <select
                      value={draft.sort}
                      onChange={(e) => setDraft({ ...draft, sort: e.target.value })}
                      className="field"
                    >
                      {sorts.map((s) => (
                        <option key={s.value} value={s.value}>
                          {s.label}
                        </option>
                      ))}
                    </select>
                  </Field>
                </div>

                <Field
                  label="Availability"
                  hint={
                    source === 'local'
                      ? 'Removed listings are kept here with their last known price.'
                      : undefined
                  }
                >
                  <SegmentedControl
                    size="sm"
                    value={draft.availability}
                    onChange={(availability) => setDraft({ ...draft, availability })}
                    options={
                      source === 'local'
                        ? [
                            { value: 'any', label: 'Any' },
                            { value: 'buyable', label: 'Buyable' },
                            { value: 'in_stock', label: 'In stock' },
                            { value: 'delisted', label: 'Removed' },
                          ]
                        : [
                            { value: 'any', label: 'Any' },
                            { value: 'in_stock', label: 'In stock' },
                            { value: 'preorder', label: 'Pre-order' },
                          ]
                    }
                  />
                </Field>

                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Min price (JPY)">
                    <input
                      type="number"
                      min={0}
                      value={draft.minPrice}
                      onChange={(e) => setDraft({ ...draft, minPrice: e.target.value })}
                      className="field tabular-nums"
                      placeholder="0"
                    />
                  </Field>
                  <Field label="Max price (JPY)">
                    <input
                      type="number"
                      min={0}
                      value={draft.maxPrice}
                      onChange={(e) => setDraft({ ...draft, maxPrice: e.target.value })}
                      className="field tabular-nums"
                      placeholder="20000"
                    />
                  </Field>
                </div>

                {source === 'local' ? (
                  <Toggle
                    checked={draft.atLowestEver}
                    onChange={(atLowestEver) => setDraft({ ...draft, atLowestEver })}
                    label="Only items at their lowest price ever"
                    hint="Measured against every price this instance has recorded, including listings the shop has since deleted."
                  />
                ) : (
                  <>
                    <Field
                      label="Exclude words"
                      hint="Comma separated. Applied to the fetched page."
                    >
                      <input
                        value={draft.exclude}
                        onChange={(e) => setDraft({ ...draft, exclude: e.target.value })}
                        className="field"
                        placeholder="keychain, acrylic, poster"
                      />
                    </Field>
                    <Toggle
                      checked={draft.onSale}
                      onChange={(onSale) => setDraft({ ...draft, onSale })}
                      label="On sale only"
                    />
                  </>
                )}
              </div>

              <div className="border-t border-line pt-4 lg:border-l lg:border-t-0 lg:pl-5 lg:pt-0">
                <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted">
                  MyFigureCollection tags
                </p>
                {source === 'local' ? (
                  <TagFilter value={tags} onChange={setTags} compact />
                ) : (
                  <p className="text-xs leading-relaxed text-faint">
                    Tag filtering needs the local index, since AmiAmi knows nothing about
                    MyFigureCollection's tags. Switch to the catalogue to use them.
                  </p>
                )}
              </div>
            </div>

            <div className="mt-4 flex justify-end gap-2 border-t border-line pt-3">
              <button
                type="button"
                onClick={() => {
                  setDraft(EMPTY)
                  setTags(EMPTY_TAGS)
                }}
                className="btn-ghost text-sm"
              >
                Reset
              </button>
              <button type="submit" className="btn-primary text-sm">
                Apply
              </button>
            </div>
          </Card>
        )}
      </form>

      {results.isError && (
        <Card className="flex items-start gap-3 border-danger/40 p-4 text-sm">
          <Icon name="alertTriangle" className="mt-0.5 h-4 w-4 text-danger" />
          <div>
            <p className="font-medium text-danger">Search failed</p>
            <p className="mt-0.5 text-muted">{(results.error as Error).message}</p>
          </div>
          <button onClick={() => void results.refetch()} className="btn-ghost ml-auto shrink-0">
            <Icon name="refresh" />
            Retry
          </button>
        </Card>
      )}

      <div className="flex flex-wrap items-center gap-3 text-sm text-muted">
        {results.isFetching ? (
          <span className="inline-flex items-center gap-2">
            <Spinner className="h-3.5 w-3.5" />
            {source === 'local' ? 'Filtering the catalogue…' : 'Asking AmiAmi…'}
          </span>
        ) : results.data ? (
          <>
            <span>
              <strong className="text-ink tabular-nums">
                {results.data.total.toLocaleString('en-GB')}
              </strong>{' '}
              {source === 'local' ? 'in the catalogue' : 'on AmiAmi'}
            </span>
            <Badge>{results.data.took_ms} ms</Badge>
            {tags.include.length > 0 && (
              <Badge tone="accent">
                {tags.mode === 'all' ? 'all' : 'any'} of {tags.include.length} tag(s)
              </Badge>
            )}
            {tags.exclude.length > 0 && (
              <Badge tone="danger">excluding {tags.exclude.length}</Badge>
            )}
          </>
        ) : null}
      </div>

      {results.isLoading ? (
        <div className="grid-cards">
          {Array.from({ length: 12 }).map((_, index) => (
            <ItemCardSkeleton key={index} />
          ))}
        </div>
      ) : results.data?.items.length ? (
        <div className="grid-cards">
          {results.data.items.map((item) => (
            <ItemCard
              key={item.id ?? item.code}
              item={item}
              onOpen={(target) => target.id && navigate(`/item/${target.id}`)}
              onWatch={(target) => setWatchSeed(target)}
              onWishlist={(target) => wishlist.mutate(target)}
            />
          ))}
        </div>
      ) : source === 'shop' && !applied.q.trim() ? (
        <EmptyState
          icon="search"
          title="Search AmiAmi directly"
          body="A live search reaches figures the catalogue has not crawled yet. For browsing, filtering and anything involving tags, the catalogue is faster and larger."
        />
      ) : (
        <EmptyState
          icon="search"
          title="Nothing matched"
          body={
            activeFilters > 0
              ? 'Loosen a filter. Excluded tags and the lowest-ever filter are the two that narrow hardest.'
              : 'Try fewer words. Character names usually work better than series names.'
          }
        />
      )}

      {results.data && results.data.pages > 1 && (
        <div className="flex items-center justify-center gap-2 pt-2">
          <button onClick={() => setPage(1)} disabled={page <= 1} className="btn-ghost px-2.5">
            <Icon name="chevronLeft" className="h-3.5 w-3.5" />
            <Icon name="chevronLeft" className="-ml-2.5 h-3.5 w-3.5" />
          </button>
          <button
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
            className="btn-ghost"
          >
            <Icon name="chevronLeft" />
            Previous
          </button>
          <span className="px-3 text-sm text-muted tabular-nums">
            Page {page.toLocaleString('en-GB')} of {results.data.pages.toLocaleString('en-GB')}
          </span>
          <button
            onClick={() => setPage((p) => p + 1)}
            disabled={page >= results.data.pages}
            className="btn-ghost"
          >
            Next
            <Icon name="chevronRight" />
          </button>
        </div>
      )}

      {watchSeed && (
        <WatchEditor
          open
          seedItem={watchSeed}
          onClose={() => setWatchSeed(null)}
          onSaved={() => {
            setWatchSeed(null)
            void queryClient.invalidateQueries({ queryKey: ['watches'] })
            toast.success(
              'Watch created',
              'The first check records what exists today; alerts start from the next change.',
            )
          }}
        />
      )}
    </div>
  )
}
