import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { api } from '@/api/client'
import type { CollectionStatus, Item } from '@/api/types'
import { useToast } from '@/lib/toast'
import { Icon, type IconName } from './Icon'
import { Spinner } from './ui'

const STATUSES: { value: CollectionStatus; label: string; hint: string; icon: IconName }[] = [
  { value: 'wishlist', label: 'Wishlist', hint: 'Want it', icon: 'heart' },
  { value: 'ordered', label: 'Ordered', hint: 'Paid, on its way', icon: 'box' },
  { value: 'owned', label: 'Owned', hint: 'On the shelf', icon: 'check' },
  { value: 'sold', label: 'Sold', hint: 'Passed it on', icon: 'yen' },
]

/**
 * Add an item to the collection, at whichever status fits.
 *
 * The heart alone only ever meant "wishlist", which left no way at all to
 * record something already owned. The main button keeps the one-click path to
 * the wishlist; the caret opens the rest.
 */
export function CollectionButton({ item, compact }: { item: Item; compact?: boolean }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)

  const current = item.in_collection

  const refresh = () => {
    for (const key of ['search', 'discover', 'items', 'item', 'collection', 'dashboard']) {
      void queryClient.invalidateQueries({ queryKey: [key] })
    }
  }

  const setStatus = useMutation({
    mutationFn: async (status: CollectionStatus | null) => {
      if (status === null) {
        if (item.collection_entry_id) await api.collection.remove(item.collection_entry_id)
        return null
      }
      if (item.collection_entry_id) {
        await api.collection.update(item.collection_entry_id, { status })
      } else {
        await api.collection.add({ item_id: item.id!, status })
      }
      return status
    },
    onSuccess: (status) => {
      toast.success(status ? `Saved as ${status}` : 'Removed from your collection')
      setOpen(false)
      refresh()
    },
    onError: (error) => toast.error('Could not save', (error as Error).message),
  })

  const active = STATUSES.find((s) => s.value === current)

  return (
    <div className="relative">
      <div className="flex">
        <button
          onClick={() => setStatus.mutate(current ? null : 'wishlist')}
          disabled={setStatus.isPending}
          aria-pressed={Boolean(current)}
          title={
            item.saved_via_counterpart
              ? `Saved as the ${item.condition === 'preowned' ? 'new' : 'pre-owned'} listing (${current}). A wishlist entry covers the figure, not one listing.`
              : current
                ? `Remove from your collection (${current})`
                : 'Add to your wishlist'
          }
          className={clsx(
            'btn-ghost rounded-r-none border-r-0',
            compact ? 'px-2.5 py-1.5 text-xs' : '',
            current && 'border-accent bg-accent/12 text-accent',
          )}
        >
          {setStatus.isPending ? (
            <Spinner className="h-4 w-4" />
          ) : (
            <Icon
              name={active?.icon ?? 'heart'}
              className={clsx('h-4 w-4', current && 'fill-current')}
            />
          )}
          {!compact && (active ? active.label : 'Wishlist')}
        </button>
        <button
          onClick={() => setOpen((v) => !v)}
          aria-label="Choose collection status"
          className={clsx(
            'btn-ghost rounded-l-none px-1.5',
            current && 'border-accent bg-accent/12 text-accent',
          )}
        >
          <Icon name="chevronDown" className="h-3.5 w-3.5" />
        </button>
      </div>

      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} aria-hidden="true" />
          <div className="absolute right-0 z-40 mt-1 w-52 overflow-hidden rounded-card border border-line bg-surface py-1 shadow-pop">
            {STATUSES.map((status) => (
              <button
                key={status.value}
                onClick={() => setStatus.mutate(status.value)}
                className={clsx(
                  'flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm transition-colors hover:bg-raised',
                  current === status.value && 'text-accent',
                )}
              >
                <Icon name={status.icon} className="h-4 w-4" />
                <span className="flex-1">
                  {status.label}
                  <span className="ml-1.5 text-xs text-faint">{status.hint}</span>
                </span>
                {current === status.value && <Icon name="check" className="h-3.5 w-3.5" />}
              </button>
            ))}
            {current && (
              <>
                <div className="my-1 border-t border-line" />
                <button
                  onClick={() => setStatus.mutate(null)}
                  className="flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-danger transition-colors hover:bg-raised"
                >
                  <Icon name="trash" className="h-4 w-4" />
                  Remove from collection
                </button>
              </>
            )}
          </div>
        </>
      )}
    </div>
  )
}
