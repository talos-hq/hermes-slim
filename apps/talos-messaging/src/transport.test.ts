import { describe, expect, it } from 'vitest'
import { isKeyRejection } from './transport'

describe('isKeyRejection', () => {
  it('recognises the gateway refusing the key', () => {
    expect(isKeyRejection(new Error('key rejected'))).toBe(true)
  })

  it('does not mistake an unreachable gateway for a refused key', () => {
    // Only an unreachable gateway may fall back to the relay: a refused key must
    // not, or a revoked phone would bounce between routes instead of asking for a
    // new link.
    expect(isKeyRejection(new Error('ticket 502'))).toBe(false)
    expect(isKeyRejection(new TypeError('Failed to fetch'))).toBe(false)
    expect(isKeyRejection('key rejected')).toBe(false)
  })
})
