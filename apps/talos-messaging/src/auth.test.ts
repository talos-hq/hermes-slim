import { describe, expect, it } from 'vitest'
import { captureKey, forgetKey, KEY_STORAGE, keyFingerprint, readKeyFromFragment, stripFragment, TOKEN_PATTERN } from './auth'

const TOKEN = 'A'.repeat(21) + 'b'.repeat(21) + '_' // 43 URL-safe characters

function memoryStorage(seed: Record<string, string> = {}) {
  const map = new Map(Object.entries(seed))
  return {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => void map.set(key, value),
    removeItem: (key: string) => void map.delete(key),
    dump: () => Object.fromEntries(map),
  }
}

describe('install key', () => {
  it('reads the key from the fragment only', () => {
    expect(readKeyFromFragment(`https://clud.bot/talos/#k=${TOKEN}`)).toBe(TOKEN)
    // A query string is sent to the server: never a key carrier.
    expect(readKeyFromFragment(`https://clud.bot/talos/?k=${TOKEN}`)).toBeNull()
    expect(readKeyFromFragment('https://clud.bot/talos/')).toBeNull()
  })

  it('refuses a malformed key instead of trusting it', () => {
    expect(TOKEN_PATTERN.test(TOKEN)).toBe(true)
    expect(readKeyFromFragment('https://clud.bot/talos/#k=short')).toBeNull()
    expect(readKeyFromFragment(`https://clud.bot/talos/#k=${TOKEN}/`)).toBeNull()
  })

  it('stores a fragment key and scrubs nothing else from the URL', () => {
    const storage = memoryStorage()
    const captured = captureKey(`https://clud.bot/talos/#k=${TOKEN}`, storage)
    expect(captured).toEqual({ token: TOKEN, source: 'fragment' })
    expect(storage.dump()[KEY_STORAGE]).toBe(TOKEN)
    expect(stripFragment(`https://clud.bot/talos/#k=${TOKEN}`)).toBe('https://clud.bot/talos/')
  })

  it('falls back to the stored key on a cold start', () => {
    const storage = memoryStorage({ [KEY_STORAGE]: TOKEN })
    expect(captureKey('https://clud.bot/talos/', storage)).toEqual({ token: TOKEN, source: 'stored' })
  })

  it('has no key without a fragment or storage', () => {
    expect(captureKey('https://clud.bot/talos/', memoryStorage())).toBeNull()
    expect(captureKey('https://clud.bot/talos/', memoryStorage({ [KEY_STORAGE]: 'nope' }))).toBeNull()
  })

  it('forgets the key on revoke', () => {
    const storage = memoryStorage({ [KEY_STORAGE]: TOKEN })
    forgetKey(storage)
    expect(storage.dump()).toEqual({})
  })

  it('never exposes the key in a fingerprint', () => {
    const fingerprint = keyFingerprint(TOKEN)
    expect(fingerprint).toMatch(/^key:[0-9a-f]{8}$/)
    expect(fingerprint).not.toContain(TOKEN.slice(0, 8))
  })
})
