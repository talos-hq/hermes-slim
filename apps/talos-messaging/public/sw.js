import { notificationURL } from './push-logic.js'

self.addEventListener('push', (event) => {
  const text = event.data?.text() || 'You have a new message.'
  let url
  try { url = JSON.parse(text).url } catch { /* Plain text is a valid push payload. */ }
  event.waitUntil(self.registration.showNotification('Talos Messaging', {
    body: text,
    icon: '/talos/icons/icon-192.png',
    data: { url: notificationURL(url, self.location.origin) },
  }))
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const url = notificationURL(event.notification.data?.url, self.location.origin)
  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    const client = clients.find((item) => new URL(item.url).origin === self.location.origin && new URL(item.url).pathname.startsWith('/talos/'))
    if (client) {
      await client.navigate(url)
      await client.focus()
    } else {
      await self.clients.openWindow(url)
    }
  })())
})
