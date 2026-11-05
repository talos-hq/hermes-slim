import { describe, expect, it } from 'vitest'
import { deviceId, pushUnavailable, serializeSubscription, shouldRefreshMailbox, vapidBytes, type PushFeatures } from './push'
// @ts-expect-error Worker module is deliberately plain JavaScript, deployed unchanged.
import { notificationURL } from '../public/push-logic.js'

const supported: PushFeatures = { serviceWorker: true, pushManager: true, notification: true, secure: true, ios: false, standalone: false }

describe('push feature matrix', () => {
  for (let mask = 0; mask < 8; mask++) {
    it(`handles API availability mask ${mask}`, () => {
      const features = { ...supported, serviceWorker: Boolean(mask & 1), pushManager: Boolean(mask & 2), notification: Boolean(mask & 4) }
      expect(pushUnavailable(features) === null).toBe(mask === 7)
    })
  }
  it('requires installation on iOS even when Safari lacks push APIs', () => {
    expect(pushUnavailable({ ...supported, ios: true, pushManager: false })).toContain('Add to Home Screen')
    expect(pushUnavailable({ ...supported, ios: true, standalone: true })).toBeNull()
  })
  it('requires HTTPS', () => expect(pushUnavailable({ ...supported, secure: false })).toContain('HTTPS'))
})

it('serializes the enrollment accepted by the gateway without browser-only fields', () => {
  expect(serializeSubscription('device', { toJSON: () => ({ endpoint: 'https://push.example/x', expirationTime: null, keys: { p256dh: 'public', auth: 'secret' } }) })).toEqual({
    device_id: 'device', subscription: { endpoint: 'https://push.example/x', keys: { p256dh: 'public', auth: 'secret' } },
  })
  expect(() => serializeSubscription('device', { toJSON: () => ({}) })).toThrow('incomplete')
})

it('retains a stable device identity', () => {
  const values = new Map<string, string>()
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value) } }
  expect(deviceId(storage)).toBe(deviceId(storage))
})

it('decodes URL-safe VAPID bytes', () => expect([...vapidBytes('-_8')]).toEqual([251, 255]))

it.each([
  [undefined, 'https://talos.example/talos/'],
  ['/talos/?message=123', 'https://talos.example/talos/?message=123'],
  ['https://evil.example/talos/', 'https://talos.example/talos/'],
  ['javascript:alert(1)', 'https://talos.example/talos/'],
  ['/api/private', 'https://talos.example/talos/'],
  ['http://[invalid', 'https://talos.example/talos/'],
])('resolves notification destination %s', (value, expected) => {
  expect(notificationURL(value, 'https://talos.example')).toBe(expected)
})

it.each([
  ['hidden', 'visible', true], ['visible', 'hidden', false],
  ['visible', 'visible', false], ['hidden', 'hidden', false],
])('refreshes mailbox on %s → %s: %s', (previous, current, expected) => {
  expect(shouldRefreshMailbox(previous, current)).toBe(expected)
})
