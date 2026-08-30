/**
 * Server-sent events.
 *
 * The backend pushes an alert the moment a watch fires, which is the entire
 * point of the app: the browser tab should know before the e-mail does.
 * EventSource reconnects on its own, so this only has to wire handlers up.
 *
 * The stream is dropped while the tab is hidden, and that is not a nicety.
 * Over HTTP/1.1 a browser allows six connections to one origin, shared across
 * every tab of it, and an EventSource holds one open for as long as it lives.
 * Six tabs of this application therefore consumed the entire allowance and
 * everything else - pages, images, API calls - queued behind streams that
 * were doing nothing. Tabs simply stopped loading.
 *
 * A hidden tab has no one to show an alert to, so it gives its connection
 * back and takes one again when it is looked at. What it missed arrives with
 * the refetch that TanStack Query already does on focus.
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

/**
 * How long a tab may be hidden before it gives up its connection.
 *
 * Long enough that flicking between two tabs does not tear the stream down
 * and build it up again on every switch; short enough that a window left in
 * the background stops holding a slot someone else needs.
 */
const RELEASE_AFTER_MS = 20_000

export function useAlertStream(enabled: boolean, onAlert: (alert: AlertEvent) => void) {
  const handler = useRef(onAlert)
  handler.current = onAlert

  useEffect(() => {
    if (!enabled) return

    let source: EventSource | null = null
    let releaseTimer: ReturnType<typeof setTimeout> | undefined

    const listener = (event: MessageEvent) => {
      try {
        handler.current(JSON.parse(event.data) as AlertEvent)
      } catch {
        /* malformed frame, nothing useful to do */
      }
    }

    function open() {
      if (source) return
      source = new EventSource('/api/alerts/stream', { withCredentials: true })
      source.addEventListener('alert', listener as EventListener)
    }

    function close() {
      if (!source) return
      source.removeEventListener('alert', listener as EventListener)
      source.close()
      source = null
    }

    function sync() {
      clearTimeout(releaseTimer)
      if (document.visibilityState === 'visible') {
        open()
      } else {
        releaseTimer = setTimeout(close, RELEASE_AFTER_MS)
      }
    }

    sync()
    document.addEventListener('visibilitychange', sync)
    // A tab being put to sleep or restored from the back/forward cache does
    // not always fire visibilitychange, and a stream left open through either
    // is a connection nobody is holding on purpose.
    window.addEventListener('pagehide', close)
    window.addEventListener('pageshow', sync)

    return () => {
      clearTimeout(releaseTimer)
      document.removeEventListener('visibilitychange', sync)
      window.removeEventListener('pagehide', close)
      window.removeEventListener('pageshow', sync)
      close()
    }
  }, [enabled])
}
