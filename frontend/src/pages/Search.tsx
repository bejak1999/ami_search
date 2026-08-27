import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '@/api/client'
import type { Item } from '@/api/types'
import { Icon } from '@/components/Icon'
import { ItemCard, ItemCardSkeleton } from '@/components/ItemCard'
import { WatchEditor } from '@/components/WatchEditor'
import { Badge, Card, EmptyState, Field, SegmentedControl, Spinner, Toggle } from '@/components/ui'
import { useToast } from '@/lib/toast'
import { useWishlistToggle } from '@/lib/useWishlist'
import clsx from 'clsx'

interface Filters {
  q: string
  condition: 'any' | 'new' | 'preowned'
  stock: 'any' | 'in_stock' | 'preorder' | 'backorder'
  sort: 'newest' | 'preowned' | 'price_asc' | 'price_desc' | 'release' | 'discount'
  min_price: string
  max_price: string
  exclude: string
  on_sale: boolean
  category_id?: number
  page: number
}

const EMPTY: Filters = {
  q: '',
  condition: 'any',
  stock: 'any',
  sort: 'newest',
  min_price: '',
  max_price: '',
  exclude: '',
  on_sale: false,
  page: 1,
}

export function SearchPage() {
  const navigate = useNavigate()
  const toast = useToast()
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const [filters, setFilters] = useState<Filters>({ ...EMPTY, q: params.get('q') ?? '' })
  const [submitted, setSubmitted] = useState<Filters | null>(
    params.get('q') ? { ...EMPTY, q: params.get('q')! } : null,
  )
  const [showFilters, setShowFilters] = useState(false)
  const [watchSeed, setWatchSeed] = useState<Partial<Item> | null>(null)

  useEffect(() => {
    const q = params.get('q')
    if (q && q !== submitted?.q) {
      const next = { ...EMPTY, q }
      setFilters(next)
      setSubmitted(next)
    }
    // Only react to the URL changing from outside this component.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params])

  const search = useQuery({
    queryKey: ['search', submitted],
    enabled: Boolean(submitted),
    queryFn: () =>
      api.search.run({
        q: submitted!.q,
        condition: submitted!.condition,
        stock: submitted!.stock,
        sort: submitted!.sort,
        page: submitted!.page,
        per_page: 40,
        min_price: submitted!.min_price ? Number(submitted!.min_price) : undefined,
        max_price: submitted!.max_price ? Number(submitted!.max_price) : undefined,
        exclude: submitted!.exclude
          ? submitted!.exclude.split(',').map((s) => s.trim()).filter(Boolean)
          : [],
        on_sale: submitted!.on_sale,
        category_id: submitted!.category_id,
      }),
    staleTime: 30_000,
  })

  const resolve = useMutation({
    mutationFn: (input: string) => api.search.resolve(input),
    onSuccess: (item) => {
      if (item.id) navigate(`/item/${item.id}`)
    },
    onError: (error) =>
      toast.error('Could not open that link', error instanceof ApiError ? error.message : undefined),
  })

  const wishlist = useWishlistToggle()

  function submit(event?: React.FormEvent) {
    event?.preventDefault()
    const term = filters.q.trim()
    // A pasted product link is almost never meant as a search term.
    if (/amiami\.com|^[A-Z]{3,}-[A-Z0-9-]+$/i.test(term)) {
      resolve.mutate(term)
      return
    }
    const next = { ...filters, page: 1 }
    setSubmitted(next)
    setParams(term ? { q: term } : {}, { replace: true })
  }

  function goToPage(page: number) {
    if (!submitted) return
    const next = { ...submitted, page }
    setSubmitted(next)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const activeFilterCount =
    (filters.condition !== 'any' ? 1 : 0) +
    (filters.stock !== 'any' ? 1 : 0) +
    (filters.min_price ? 1 : 0) +
    (filters.max_price ? 1 : 0) +
    (filters.exclude ? 1 : 0) +
    (filters.on_sale ? 1 : 0)

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Search</h1>
        <p className="mt-1 text-sm text-muted">
          Search AmiAmi live, or paste a product link to jump straight to an item.
        </p>
      </header>

      <form onSubmit={submit} className="space-y-3">
        <div className="flex gap-2">
          <div className="relative flex-1">
            <Icon
              name="search"
              className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-faint"
            />
            <input
              value={filters.q}
              onChange={(e) => setFilters({ ...filters, q: e.target.value })}
              placeholder="Nendoroid Hatsune Miku, or paste an amiami.com link"
              className="field h-11 pl-10 text-[15px]"
              autoFocus
            />
            {resolve.isPending && (
              <Spinner className="absolute right-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-accent" />
            )}
          </div>
          <button
            type="button"
            onClick={() => setShowFilters((v) => !v)}
            className={clsx('btn-ghost h-11 px-3.5', activeFilterCount > 0 && 'border-accent/50 text-accent')}
          >
            <Icon name="filter" />
            <span className="hidden sm:inline">Filters</span>
            {activeFilterCount > 0 && (
              <span className="rounded-full bg-accent px-1.5 text-[10px] font-bold text-accent-ink">
                {activeFilterCount}
              </span>
            )}
          </button>
          <button type="submit" className="btn-primary h-11 px-5">
            Search
          </button>
        </div>

        {showFilters && (
          <Card className="animate-fade-up space-y-4 p-4">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Field label="Condition">
                <SegmentedControl
                  size="sm"
                  value={filters.condition}
                  onChange={(condition) => setFilters({ ...filters, condition })}
                  options={[
                    { value: 'any', label: 'Any' },
                    { value: 'new', label: 'New' },
                    { value: 'preowned', label: 'Pre-owned' },
                  ]}
                />
              </Field>
              <Field label="Availability">
                <SegmentedControl
                  size="sm"
                  value={filters.stock}
                  onChange={(stock) => setFilters({ ...filters, stock })}
                  options={[
                    { value: 'any', label: 'Any' },
                    { value: 'in_stock', label: 'In stock' },
                    { value: 'preorder', label: 'Pre-order' },
                  ]}
                />
              </Field>
              <Field label="Min price (JPY)">
                <input
                  type="number"
                  min={0}
                  value={filters.min_price}
                  onChange={(e) => setFilters({ ...filters, min_price: e.target.value })}
                  className="field"
                  placeholder="0"
                />
              </Field>
              <Field label="Max price (JPY)">
                <input
                  type="number"
                  min={0}
                  value={filters.max_price}
                  onChange={(e) => setFilters({ ...filters, max_price: e.target.value })}
                  className="field"
                  placeholder="20000"
                />
              </Field>
            </div>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Field
                label="Exclude words"
                hint="Comma separated. Useful for filtering out acrylic stands and keychains."
                className="sm:col-span-2"
              >
                <input
                  value={filters.exclude}
                  onChange={(e) => setFilters({ ...filters, exclude: e.target.value })}
                  className="field"
                  placeholder="keychain, acrylic, poster"
                />
              </Field>
              <Field label="Sort">
                <select
                  value={filters.sort}
                  onChange={(e) => setFilters({ ...filters, sort: e.target.value as Filters['sort'] })}
                  className="field"
                >
                  <option value="newest">Newest first</option>
                  <option value="preowned">Pre-owned first</option>
                  <option value="price_asc">Price, low to high</option>
                  <option value="price_desc">Price, high to low</option>
                  <option value="release">Release date</option>
                  <option value="discount">Biggest discount</option>
                </select>
              </Field>
              <div className="flex items-end pb-1">
                <Toggle
                  checked={filters.on_sale}
                  onChange={(on_sale) => setFilters({ ...filters, on_sale })}
                  label="On sale only"
                />
              </div>
            </div>
            <p className="text-xs text-faint">
              Price, exclusions and most sort orders are applied to the current page of results;
              AmiAmi itself only sorts by newest and pre-owned.
            </p>
          </Card>
        )}
      </form>

      {search.isError && (
        <Card className="flex items-start gap-3 border-danger/40 p-4 text-sm">
          <Icon name="alertTriangle" className="mt-0.5 h-4 w-4 text-danger" />
          <div>
            <p className="font-medium text-danger">Search failed</p>
            <p className="mt-0.5 text-muted">{(search.error as Error).message}</p>
          </div>
          <button onClick={() => void search.refetch()} className="btn-ghost ml-auto shrink-0">
            <Icon name="refresh" />
            Retry
          </button>
        </Card>
      )}

      {submitted && (
        <>
          <div className="flex flex-wrap items-center gap-3 text-sm text-muted">
            {search.isFetching ? (
              <span className="inline-flex items-center gap-2">
                <Spinner className="h-3.5 w-3.5" />
                Searching AmiAmi…
              </span>
            ) : search.data ? (
              <>
                <span>
                  <strong className="text-ink tabular-nums">
                    {search.data.total.toLocaleString('en-GB')}
                  </strong>{' '}
                  results
                </span>
                <Badge>{search.data.took_ms} ms</Badge>
                {search.data.items.length < search.data.per_page && (
                  <span className="text-xs text-faint">
                    Some results on this page were filtered out locally.
                  </span>
                )}
              </>
            ) : null}
          </div>

          {search.isLoading ? (
            <div className="grid-cards">
              {Array.from({ length: 12 }).map((_, index) => (
                <ItemCardSkeleton key={index} />
              ))}
            </div>
          ) : search.data?.items.length ? (
            <div className="grid-cards">
              {search.data.items.map((item) => (
                <ItemCard
                  key={item.code}
                  item={item}
                  onOpen={(target) => target.id && navigate(`/item/${target.id}`)}
                  onWatch={(target) => setWatchSeed(target)}
                  onWishlist={(target) => wishlist.mutate(target)}
                />
              ))}
            </div>
          ) : (
            <EmptyState
              icon="search"
              title="Nothing matched"
              body="Try fewer words, or loosen the filters. AmiAmi matches on the English product title, so character names usually work better than series names."
            />
          )}

          {search.data && search.data.pages > 1 && (
            <div className="flex items-center justify-center gap-2 pt-2">
              <button
                onClick={() => goToPage(submitted.page - 1)}
                disabled={submitted.page <= 1}
                className="btn-ghost"
              >
                <Icon name="chevronLeft" />
                Previous
              </button>
              <span className="px-3 text-sm text-muted tabular-nums">
                Page {submitted.page} of {search.data.pages.toLocaleString('en-GB')}
              </span>
              <button
                onClick={() => goToPage(submitted.page + 1)}
                disabled={submitted.page >= search.data.pages}
                className="btn-ghost"
              >
                Next
                <Icon name="chevronRight" />
              </button>
            </div>
          )}
        </>
      )}

      {!submitted && (
        <EmptyState
          icon="search"
          title="Search AmiAmi"
          body="Look for a character, a series or a product code. Pasting an amiami.com link opens that item directly, with its full price history."
        />
      )}

      {watchSeed && (
        <WatchEditor
          open
          seedItem={watchSeed}
          onClose={() => setWatchSeed(null)}
          onSaved={() => {
            setWatchSeed(null)
            void queryClient.invalidateQueries({ queryKey: ['watches'] })
            toast.success('Watch created', 'The first check records what exists today; alerts start from the next change.')
          }}
        />
      )}
    </div>
  )
}
