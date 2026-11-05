/**
 * The install key: where it comes from, where it lives, and how it leaves.
 *
 * `hermes talos-messaging` prints an install link whose **fragment** carries the
 * key (`/talos/#k=<token>`). A fragment never reaches the server, its access
 * log or a proxy, so the key is captured on the device, kept in localStorage so
 * a cold start does not need the link again, and scrubbed from the address bar
 * so a screenshot or a shared URL does not carry it.
 */

export const KEY_STORAGE = 'talos-messaging:key'
/** The token shape the gateway mints: 43 URL-safe characters. */
export const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/

export type KeySource = 'fragment' | 'stored'

export interface CapturedKey {
  token: string
  source: KeySource
}

/** Read `#k=<token>` out of an href. `null` when absent or malformed. */
export function readKeyFromFragment(href: string): string | null {
  const hash = href.includes('#') ? href.slice(href.indexOf('#') + 1) : ''
  if (!hash) return null
  const token = new URLSearchParams(hash).get('k')
  return token && TOKEN_PATTERN.test(token) ? token : null
}

/** The URL without the fragment, for a history replace. */
export function stripFragment(href: string): string {
  const index = href.indexOf('#')
  return index === -1 ? href : href.slice(0, index)
}

/**
 * Capture the key from the URL if present, else fall back to the stored one.
 * A fragment wins over storage: a new link re-installs a revoked key.
 */
export function captureKey(href: string, storage: Pick<Storage, 'getItem' | 'setItem'>): CapturedKey | null {
  const fromFragment = readKeyFromFragment(href)
  if (fromFragment) {
    storage.setItem(KEY_STORAGE, fromFragment)
    return { token: fromFragment, source: 'fragment' }
  }
  const stored = storage.getItem(KEY_STORAGE)
  return stored && TOKEN_PATTERN.test(stored) ? { token: stored, source: 'stored' } : null
}

/** Forget the key (revoked, or the user signed out). */
export function forgetKey(storage: Pick<Storage, 'removeItem'>): void {
  storage.removeItem(KEY_STORAGE)
}

/** A key is never logged; this is what a log line may carry instead. */
export function keyFingerprint(token: string): string {
  let hash = 0
  for (let index = 0; index < token.length; index += 1) {
    hash = (hash * 31 + token.charCodeAt(index)) >>> 0
  }
  return `key:${hash.toString(16).padStart(8, '0')}`
}
