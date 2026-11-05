import { describe, expect, it } from 'vitest'
import { localMessageId, readPreference, reconcile, writePreference, type Message } from './store'

const base = { sessionId: 's1', at: '2026-11-04T10:00:00Z' }

function message(over: Partial<Message> = {}): Message {
  return { id: 'm1', role: 'user', text: 'hello', ...base, ...over } as Message
}

describe('reconcile', () => {
  it('keeps a message only the device holds', () => {
    const local = [message({ id: 'local-1', pending: true, text: 'sent offline' })]
    expect(reconcile(local, [])).toEqual(local)
  })

  it('drops a pending message once the server holds the same text', () => {
    const local = [message({ id: 'local-1', pending: true, text: 'hi' })]
    const remote = [message({ id: 'srv-1', text: 'hi' })]
    expect(reconcile(local, remote).map((m) => m.id)).toEqual(['srv-1'])
  })

  it('prefers the server copy of the same id', () => {
    const local = [message({ id: 'm1', text: 'draft' })]
    const remote = [message({ id: 'm1', text: 'final' })]
    expect(reconcile(local, remote)).toEqual([message({ id: 'm1', text: 'final' })])
  })

  it('orders by time, and keeps trips between roles distinguishable', () => {
    const local = [message({ id: 'b', at: '2026-11-04T10:00:02Z' })]
    const remote = [message({ id: 'a', at: '2026-11-04T10:00:01Z', role: 'agent' })]
    expect(reconcile(local, remote).map((m) => m.id)).toEqual(['a', 'b'])
  })
})

describe('preferences', () => {
  it('round-trips and namespaces its keys', () => {
    const map = new Map<string, string>()
    const storage = { getItem: (k: string) => map.get(k) ?? null, setItem: (k: string, v: string) => void map.set(k, v) }
    writePreference(storage, 'theme', 'dark')
    expect(readPreference(storage, 'theme')).toBe('dark')
    expect([...map.keys()]).toEqual(['talos-messaging:theme'])
  })
})

describe('local ids', () => {
  it('are prefixed so they never collide with a server id', () => {
    expect(localMessageId().startsWith('local-')).toBe(true)
  })
})
