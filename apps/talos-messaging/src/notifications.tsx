import { useEffect, useState } from 'react'
import { keyFingerprint } from './auth'
import { api, browserFeatures, deviceId, pushUnavailable, serializeSubscription, vapidBytes } from './push'

export function Notifications({ token }: { token: string }): React.ReactElement {
  const enrollmentKey = `talos-messaging:push-enrolled:${keyFingerprint(token)}`
  const [reason] = useState(() => pushUnavailable(browserFeatures()))
  const [registration, setRegistration] = useState<ServiceWorkerRegistration | null>(null)
  const [subscribed, setSubscribed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  useEffect(() => {
    if (reason) return
    let disposed = false
    void (async () => {
      try {
        await navigator.serviceWorker.register('/talos/sw.js', { type: 'module', scope: '/talos/' })
        const ready = await navigator.serviceWorker.ready
        const subscription = await ready.pushManager.getSubscription()
        if (!disposed) { setRegistration(ready); setSubscribed(Boolean(subscription) && localStorage.getItem(enrollmentKey) === subscription?.endpoint) }
      } catch { if (!disposed) setMessage('Notifications could not start. Reload to retry.') }
    })()
    return () => { disposed = true }
  }, [reason, enrollmentKey])

  async function enable() {
    setBusy(true)
    setMessage('')
    try {
      // Permission must be requested directly from the button gesture, before network work.
      const permission = await Notification.requestPermission()
      if (permission !== 'granted') { setMessage('Notifications are blocked. Allow them in your browser or device settings.'); return }
      const { public_key } = await (await api(token, '/api/push/vapid-public-key')).json()
      const subscription = await registration!.pushManager.getSubscription() ?? await registration!.pushManager.subscribe({
        userVisibleOnly: true, applicationServerKey: vapidBytes(public_key),
      })
      await api(token, '/api/push/subscribe', { method: 'POST', body: JSON.stringify(serializeSubscription(deviceId(localStorage), subscription)) })
      localStorage.setItem(enrollmentKey, subscription.endpoint)
      setSubscribed(true)
      setMessage('Notifications enabled.')
    } catch { setMessage('Could not enable notifications. Please retry.') }
    finally { setBusy(false) }
  }

  async function disable() {
    setBusy(true)
    try {
      await api(token, `/api/push/subscribe?device_id=${encodeURIComponent(deviceId(localStorage))}`, { method: 'DELETE' })
      const subscription = await registration!.pushManager.getSubscription()
      if (subscription && !await subscription.unsubscribe()) throw new Error('Unsubscribe failed')
      localStorage.removeItem(enrollmentKey)
      setSubscribed(false)
      setMessage('Notifications disabled.')
    } catch { setMessage('Could not disable notifications. Please retry.') }
    finally { setBusy(false) }
  }

  return <section className="notifications" aria-label="Notifications">
    {reason ? <p>{reason}</p> : <button disabled={!registration || busy} onClick={() => void (subscribed ? disable() : enable())}>
      {subscribed ? 'Disable Notifications' : 'Enable Notifications'}
    </button>}
    <p role="status">{message}</p>
  </section>
}
