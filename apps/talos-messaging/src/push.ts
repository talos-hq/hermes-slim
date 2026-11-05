export interface PushFeatures {
  serviceWorker: boolean
  pushManager: boolean
  notification: boolean
  secure: boolean
  ios: boolean
  standalone: boolean
}

export function pushUnavailable(features: PushFeatures): string | null {
  if (features.ios && !features.standalone) return 'On iPhone or iPad, use Share → Add to Home Screen first, then open the installed app to enable notifications.'
  if (!features.secure) return 'Notifications require a secure HTTPS connection.'
  const missing = [!features.serviceWorker && 'service workers', !features.pushManager && 'PushManager', !features.notification && 'Notification'].filter(Boolean)
  return missing.length ? `Notifications are unavailable: this browser does not support ${missing.join(', ')}.` : null
}

export function browserFeatures(): PushFeatures {
  return {
    serviceWorker: 'serviceWorker' in navigator,
    pushManager: 'PushManager' in window,
    notification: 'Notification' in window,
    secure: window.isSecureContext,
    ios: /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1),
    standalone: window.matchMedia('(display-mode: standalone)').matches || Boolean((navigator as Navigator & { standalone?: boolean }).standalone),
  }
}

export function serializeSubscription(deviceId: string, subscription: Pick<PushSubscription, 'toJSON'>) {
  const { endpoint, keys } = subscription.toJSON()
  if (!endpoint || !keys?.p256dh || !keys.auth) throw new Error('The browser returned an incomplete push subscription.')
  return { device_id: deviceId, subscription: { endpoint, keys: { p256dh: keys.p256dh, auth: keys.auth } } }
}

export function deviceId(storage: Pick<Storage, 'getItem' | 'setItem'>): string {
  const key = 'talos-messaging:device-id'
  const existing = storage.getItem(key)
  if (existing) return existing
  const id = crypto.randomUUID()
  storage.setItem(key, id)
  return id
}

export function shouldRefreshMailbox(previous: string, current: string): boolean {
  return previous !== 'visible' && current === 'visible'
}

export async function api(token: string, path: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(path, {
    ...init,
    headers: { 'X-Talos-Messaging-Key': token, 'Content-Type': 'application/json', ...init.headers },
  })
  if (!response.ok) throw new Error(`Request failed (${response.status}).`)
  return response
}

export function vapidBytes(key: string): Uint8Array<ArrayBuffer> {
  const raw = atob(key.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - key.length % 4) % 4))
  return Uint8Array.from(raw, (character) => character.charCodeAt(0))
}
