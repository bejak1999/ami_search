/* AmiSearch service worker: Web Push only. */

self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))

self.addEventListener('push', (event) => {
  let payload = {}
  try {
    payload = event.data ? event.data.json() : {}
  } catch (error) {
    payload = { title: 'AmiSearch', body: event.data ? event.data.text() : '' }
  }

  const body = [payload.body, payload.price, payload.landed ? payload.landed + ' landed' : null]
    .filter(Boolean)
    .join(' \u00b7 ')

  event.waitUntil(
    self.registration.showNotification(payload.title || 'AmiSearch', {
      body,
      icon: payload.icon || '/icons/icon-192.png',
      badge: payload.badge || '/icons/badge-72.png',
      image: payload.image || undefined,
      // Grouping by trigger stops a burst of restocks from stacking up.
      tag: payload.tag || 'amisearch',
      renotify: true,
      requireInteraction: Boolean(payload.requireInteraction),
      data: { url: payload.url || '/' },
      actions: payload.url ? [{ action: 'open', title: 'Open on shop' }] : [],
    }),
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const target = event.notification.data && event.notification.data.url
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      // Reuse an open tab when there is one; nobody wants ten AmiSearch tabs.
      for (const client of clientList) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          client.focus()
          if (target && target.startsWith('http')) return self.clients.openWindow(target)
          return undefined
        }
      }
      return self.clients.openWindow(target || '/')
    }),
  )
})
