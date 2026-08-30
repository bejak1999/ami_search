import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import clsx from 'clsx'
import { api } from '@/api/client'
import { Icon } from './Icon'
import { SegmentedControl } from './ui'

export const TAG_KINDS: { value: string; label: string }[] = [
  { value: '', label: 'All' },
  { value: 'tag', label: 'Tags' },
  { value: 'origin', label: 'Series' },
  { value: 'character', label: 'Characters' },
  { value: 'company', label: 'Makers' },
  { value: 'artist', label: 'Sculptors' },
]

export interface TagSelection {
  include: string[]
  exclude: string[]
  mode: 'all' | 'any'
}

export const EMPTY_TAGS: TagSelection = { include: [], exclude: [], mode: 'all' }

/**
 * Picking MyFigureCollection tags to search by, and to search against.
 *
 * Excluding matters as much as including: hunting a scale figure from one
 * series means saying "this series, but no nendoroids", and a filter that can
 * only add terms cannot express that.
 */
export function TagFilter({
  value,
  onChange,
  compact,
}: {
  value: TagSelection
  onChange: (next: TagSelection) => void
  compact?: boolean
}) {
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState('')

  // The typed term goes to the server rather than filtering what came back.
  // Filtering here searched only the tags already fetched, which is the most
  // used few hundred - fine while that was most of them, useless once
  // MyFigureCollection linking had filled the table, and silent either way:
  // an unmatched tag looks identical to a tag that does not exist.
  const search = useDebounced(query.trim(), 200)

  const tags = useQuery({
    queryKey: ['localTags', kind, search],
    queryFn: () =>
      api.search.localTags({
        kind: kind || undefined,
        q: search || undefined,
        limit: 300,
      }),
    staleTime: 120_000,
    // Keep the previous list on screen while the next one loads, so typing
    // does not flash the panel empty between keystrokes.
    placeholderData: (previous) => previous,
  })

  const all = tags.data ?? []
  const visible = useMemo(
    () => (search ? all.slice(0, 60) : all.slice(0, compact ? 30 : 60)),
    [all, search, compact],
  )

  const state = (slug: string): 'include' | 'exclude' | null =>
    value.include.includes(slug) ? 'include' : value.exclude.includes(slug) ? 'exclude' : null

  /** Click cycles a tag through wanted, unwanted and unset. */
  function cycle(slug: string) {
    const current = state(slug)
    const include = value.include.filter((s) => s !== slug)
    const exclude = value.exclude.filter((s) => s !== slug)
    if (current === null) onChange({ ...value, include: [...include, slug], exclude })
    else if (current === 'include') onChange({ ...value, include, exclude: [...exclude, slug] })
    else onChange({ ...value, include, exclude })
  }

  const chosen = value.include.length + value.exclude.length

  return (
    <div className="space-y-3">
      <div className="relative">
        <Icon
          name="search"
          className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-faint"
        />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Filter tags: nendoroid, twintails, Good Smile…"
          className="field pl-9 text-sm"
        />
      </div>

      {search.length > 0 && (
        <p className="text-[11px] text-faint">
          {all.length === 0
            ? `Nothing matching \u201c${search}\u201d`
            : all.length > visible.length
              ? `Showing ${visible.length} of ${all.length} matches, most used first`
              : `${all.length} match${all.length === 1 ? '' : 'es'}`}
        </p>
      )}

      <div className="scroll-x flex gap-1.5 pb-1">
        {TAG_KINDS.map((entry) => (
          <button
            key={entry.value}
            onClick={() => setKind(entry.value)}
            className={clsx('chip shrink-0', kind === entry.value && 'chip-active')}
          >
            {entry.label}
          </button>
        ))}
      </div>

      {chosen > 0 && (
        <div className="space-y-2 rounded-control border border-line bg-raised p-2.5">
          {value.include.length > 0 && (
            <div>
              <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted">
                Must have
              </p>
              <div className="flex flex-wrap gap-1.5">
                {value.include.map((slug) => (
                  <button key={slug} onClick={() => cycle(slug)} className="chip chip-active">
                    {slug.replace(/_/g, ' ').replace(/___$/, '')}
                    <Icon name="close" className="h-3 w-3" />
                  </button>
                ))}
              </div>
            </div>
          )}

          {value.exclude.length > 0 && (
            <div>
              <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted">
                Must not have
              </p>
              <div className="flex flex-wrap gap-1.5">
                {value.exclude.map((slug) => (
                  <button
                    key={slug}
                    onClick={() => cycle(slug)}
                    className="chip border-danger/50 bg-danger/10 text-danger"
                  >
                    {slug.replace(/_/g, ' ').replace(/___$/, '')}
                    <Icon name="close" className="h-3 w-3" />
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="flex flex-wrap items-center gap-2 pt-1">
            {value.include.length > 1 && (
              <SegmentedControl
                size="sm"
                value={value.mode}
                onChange={(mode) => onChange({ ...value, mode })}
                options={[
                  { value: 'all', label: 'All of them' },
                  { value: 'any', label: 'Any of them' },
                ]}
              />
            )}
            <button
              onClick={() => onChange(EMPTY_TAGS)}
              className="btn-quiet ml-auto px-2 py-1 text-xs"
            >
              Clear
            </button>
          </div>
        </div>
      )}

      <div className={clsx('overflow-y-auto pr-1', compact ? 'max-h-52' : 'max-h-72')}>
        {tags.isLoading ? (
          <p className="py-6 text-center text-sm text-faint">Loading tags…</p>
        ) : visible.length === 0 ? (
          <p className="py-6 text-center text-xs leading-relaxed text-faint">
            No tags yet. They arrive as the catalogue is cross-referenced with
            MyFigureCollection, which runs in the background.
          </p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {visible.map((tag) => {
              const s = state(tag.slug)
              return (
                <button
                  key={`${tag.kind}-${tag.slug}`}
                  onClick={() => cycle(tag.slug)}
                  title={
                    s === 'include'
                      ? 'Click again to exclude'
                      : s === 'exclude'
                        ? 'Click again to clear'
                        : 'Click to require, twice to exclude'
                  }
                  className={clsx(
                    'chip',
                    s === 'include' && 'chip-active',
                    s === 'exclude' && 'border-danger/50 bg-danger/10 text-danger line-through',
                  )}
                >
                  {s === 'exclude' && <Icon name="close" className="h-3 w-3" />}
                  {s === 'include' && <Icon name="check" className="h-3 w-3" />}
                  {tag.name}
                  {tag.usage_count ? <span className="text-faint">{tag.usage_count}</span> : null}
                </button>
              )
            })}
          </div>
        )}
      </div>

      <p className="text-[11px] leading-relaxed text-faint">
        Click a tag to require it, click again to exclude it, once more to clear.
      </p>
    </div>
  )
}

/**
 * Hold a value still until the typing stops.
 *
 * Without this every keystroke is its own request, and the answers can arrive
 * out of order - so the list settles on whichever query the network happened
 * to finish last rather than on what was typed.
 */
function useDebounced<T>(value: T, ms: number): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), ms)
    return () => clearTimeout(timer)
  }, [value, ms])
  return settled
}
