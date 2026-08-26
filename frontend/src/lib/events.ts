/**
 * Server-sent events.
 *
 * The backend pushes an alert the moment a watch fires, which is the entire
 * point of the app: the browser tab should know before the e-mail does.
 * EventSource reconnects on its own, so this only has to wire handlers up.
 */
import { useEffect, useRef } from 'react'

export interface AlertEvent {
  id: number
  trigger: string
  title: string
  item_name?: string | null
  price: number | null
  currency: string
  landed_price: number | null
  landed_currency: string
  image_url: string | null
  url: string | null
  item_id: number | null
  watch_id: number | null
  created_at: string
  suppressed?: boolean
}

export function useAlertStream(enabled: boolean, onAlert: (alert: AlertEvent) => void) {
  const handler = useRef(onAlert)
  handler.current = onAlert

  useEffect(() => {
    if (!enabled) return
    const source = new EventSource('/api/alerts/stream', { withCredentials: true })

    const listener = (event: MessageEvent) => {
      try {
        handler.current(JSON.parse(event.data) as AlertEvent)
      } catch {
        /* malformed frame, nothing useful to do */
      }
    }

    source.addEventListener('alert', listener as EventListener)
    return () => {
      source.removeEventListener('alert', listener as EventListener)
      source.close()
    }
  }, [enabled])
}
