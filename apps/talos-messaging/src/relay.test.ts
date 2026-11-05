import { describe, expect, it } from 'vitest'
import { fetchRelayTemplate, openRelaySocket, relaySocketUrl, roomFor, SIGNAL_DOC_URL } from './relay'

const KEY = 'K'.repeat(43)

function fakeFetch(body: unknown, ok = true): typeof fetch {
  return (async (_url: string) => ({ ok, json: async () => body })) as unknown as typeof fetch
}

function fakeSocket(log: string[]): { factory: (url: string) => WebSocket; opened: () => string } {
  let url = ''
  const factory = (target: string): WebSocket => {
    url = target
    const listeners: Record<string, Array<() => void>> = {}
    const socket = {
      addEventListener: (name: string, handler: () => void) => {
        ;(listeners[name] ??= []).push(handler)
        if (name === 'open') queueMicrotask(() => handler())
      },
      removeEventListener: () => {},
      close: () => {},
      send: () => {},
      readyState: 1,
    }
    log.push(`opened ${target}`)
    return socket as unknown as WebSocket
  }
  return { factory, opened: () => url }
}

describe('roomFor', () => {
  it('is sha256(key) truncated to 32 hex characters', async () => {
    const room = await roomFor(KEY)
    expect(room).toMatch(/^[0-9a-f]{32}$/)
    // Same key, same room; a different key, a different room.
    expect(await roomFor(KEY)).toBe(room)
    expect(await roomFor('K'.repeat(42) + 'J')).not.toBe(room)
    // The key itself is never part of the room.
    expect(room).not.toContain(KEY.slice(0, 8))
  })

  it('matches the value the python bridge derives for the same key', async () => {
    // sha256("K" * 43)[:32], computed with python's hashlib — the agent side
    // (hermes_cli/talos_messaging_serve.py:room_for) must land in the same room.
    expect(await roomFor(KEY)).toBe('7a97835b001a7648b60ef502bc760f5e')
  })
})

describe('relaySocketUrl', () => {
  it('fills the room from the published template and asks for a quiet seat', () => {
    const template = 'wss://clud.bot/talos-messaging/signal/{room}?role=agent|phone'
    expect(relaySocketUrl(template, 'a'.repeat(32))).toBe(
      `wss://clud.bot/talos-messaging/signal/${'a'.repeat(32)}?role=phone&quiet=1`,
    )
    expect(relaySocketUrl(template, 'a'.repeat(32), 'agent')).toContain('role=agent')
  })
})

describe('fetchRelayTemplate', () => {
  it('reads the websocket template from the published document', async () => {
    const template = await fetchRelayTemplate(
      fakeFetch({ websocket: 'wss://clud.bot/talos-messaging/signal/{room}?role=agent|phone' }),
    )
    expect(template).toBe('wss://clud.bot/talos-messaging/signal/{room}?role=agent|phone')
  })

  it('is null when the document is missing, wrong-shaped or unreachable', async () => {
    expect(await fetchRelayTemplate(fakeFetch({}, true))).toBeNull()
    expect(await fetchRelayTemplate(fakeFetch({ websocket: 'wss://x/y' }, true))).toBeNull()
    const failing = (async () => {
      throw new Error('offline')
    }) as unknown as typeof fetch
    expect(await fetchRelayTemplate(failing)).toBeNull()
  })

  it('asks the published address', () => {
    expect(SIGNAL_DOC_URL).toBe('https://talosbot.ai/talos-messaging/signal.json')
  })
})

describe('openRelaySocket', () => {
  it('opens the quiet phone seat for the key it was given', async () => {
    const log: string[] = []
    const { factory, opened } = fakeSocket(log)
    const socket = await openRelaySocket(
      KEY,
      factory,
      fakeFetch({ websocket: 'wss://clud.bot/talos-messaging/signal/{room}?role=agent|phone' }),
    )
    expect(socket).toBeTruthy()
    expect(opened()).toContain(await roomFor(KEY))
    expect(opened()).toContain('role=phone')
    expect(opened()).toContain('quiet=1')
  })

  it('fails loudly when the relay cannot be found', async () => {
    const { factory } = fakeSocket([])
    await expect(openRelaySocket(KEY, factory, fakeFetch({}, false))).rejects.toThrow('relay unavailable')
  })
})
