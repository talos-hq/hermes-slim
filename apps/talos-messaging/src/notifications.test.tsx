import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, expect, it, vi } from 'vitest'
import { Notifications } from './notifications'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
afterEach(() => { vi.unstubAllGlobals() })

it('keeps enrollment retry available after POST fails and the app reloads', async () => {
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
  })
  const subscription = { endpoint: 'https://push.example/device', toJSON: () => ({ endpoint: 'https://push.example/device', keys: { p256dh: 'public', auth: 'auth' } }) }
  const registration = { pushManager: { getSubscription: vi.fn().mockResolvedValue(subscription) } }
  vi.stubGlobal('isSecureContext', true)
  vi.stubGlobal('matchMedia', () => ({ matches: false }))
  vi.stubGlobal('PushManager', class {})
  const permission = vi.fn().mockResolvedValue('granted')
  vi.stubGlobal('Notification', { requestPermission: permission })
  Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: { register: vi.fn().mockResolvedValue(registration), ready: Promise.resolve(registration) } })
  const fetchMock = vi.fn().mockImplementation(async (path: string) => ({
    ok: path.includes('vapid'), status: path.includes('vapid') ? 200 : 503,
    json: async () => ({ public_key: 'AQID' }),
  }))
  vi.stubGlobal('fetch', fetchMock)
  const host = document.createElement('div')
  let root = createRoot(host)
  try {
    await act(async () => { root.render(<Notifications token="test" />) })
    expect(permission).not.toHaveBeenCalled()
    await act(async () => { host.querySelector('button')!.click() })
    expect(host.textContent).toContain('Could not enable')
    await act(async () => { root.unmount() })
    root = createRoot(host)
    await act(async () => { root.render(<Notifications token="test" />) })
    expect(host.querySelector('button')!.textContent).toBe('Enable Notifications')
    fetchMock.mockResolvedValue({ ok: true, json: async () => ({ public_key: 'AQID' }) })
    await act(async () => { host.querySelector('button')!.click() })
    expect(host.querySelector('button')!.textContent).toBe('Disable Notifications')
  } finally {
    await act(async () => { root.unmount() })
    Reflect.deleteProperty(navigator, 'serviceWorker')
  }
})
