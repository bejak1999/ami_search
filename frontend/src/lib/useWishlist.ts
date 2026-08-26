import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { Item } from '@/api/types'
import { useToast } from './toast'

/**
 * Wishlist toggling.
 *
 * The heart button has to work both ways. Adding is a POST; removing needs the
 * collection entry's id, which the item payload carries precisely so the UI
 * does not have to go looking for it.
 */
export function useWishlistToggle() {
  const toast = useToast()
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: async (item: Item) => {
      if (item.collection_entry_id) {
        await api.collection.remove(item.collection_entry_id)
        return 'removed' as const
      }
      await api.collection.add({ item_id: item.id!, status: 'wishlist' })
      return 'added' as const
    },
    onSuccess: (action) => {
      toast.success(action === 'added' ? 'Added to your wishlist' : 'Removed from your wishlist')
      // Anything showing an item card carries the collection flag with it.
      for (const key of ['search', 'discover', 'items', 'item', 'collection', 'dashboard']) {
        void queryClient.invalidateQueries({ queryKey: [key] })
      }
    },
    onError: (error) => toast.error('Could not update your wishlist', (error as Error).message),
  })
}
