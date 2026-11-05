/**
 * The relay path: how the phone reaches its agent when the gateway is not on
 * the same network.
 *
 * A phone on cellular cannot dial the machine's gateway. Both ends dial *out*
 * to the public relay instead, which introduces them in a room and forwards
 * frames between the two seats. The room is `sha256(key)[:32]`, so only a
 * holder of this installation's key can be in it, and the relay never sees the
 * key itself.
 *
 * The phone dials with `?quiet=1`: the relay's own `peer_joined`/`peer_left`
 * chatter would otherwise be handed to the JSON-RPC client as protocol traffic.
 */

/** The published address of the relay (talosbot.ai is static: it names, it does not serve). */
export const SIGNAL_DOC_URL = 'https://talosbot.ai/talos-messaging/signal.json'
/** How long to wait for the published document before giving up on the relay path. */
const DOC_TIMEOUT_MS = 4000

/** Hex-encoded `sha256(key)`, first 32 characters — the relay's room id. */
export async function roomFor(key: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(key))
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('').slice(0, 32)
}

/** `wss://…/signal/{room}?role=phone&quiet=1` from the document's template. */
export function relaySocketUrl(template: string, room: string, role: 'agent' | 'phone' = 'phone'): string {
  const base = template.split('{room}')[0].replace(/\/$/, '')
  return `${base}/${room}?role=${role}&quiet=1`
}

/** The relay's socket template, or null when the document is unreachable. */
export async function fetchRelayTemplate(fetchImpl: typeof fetch = fetch): Promise<string | null> {
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), DOC_TIMEOUT_MS)
    const response = await fetchImpl(SIGNAL_DOC_URL, { signal: controller.signal })
    clearTimeout(timer)
    if (!response.ok) return null
    const body = (await response.json()) as { websocket?: unknown }
    const template = typeof body.websocket === 'string' ? body.websocket : ''
    return template.includes('{room}') ? template : null
  } catch {
    return null
  }
}

/**
 * Open the phone's seat and return the socket, ready to be handed to the
 * JSON-RPC client (it speaks plain text frames, which the relay forwards).
 */
export async function openRelaySocket(
  key: string,
  openSocket: (url: string) => WebSocket = (url) => new WebSocket(url),
  fetchImpl: typeof fetch = fetch,
): Promise<WebSocket> {
  const template = await fetchRelayTemplate(fetchImpl)
  if (!template) throw new Error('relay unavailable')
  const socket = openSocket(relaySocketUrl(template, await roomFor(key)))
  await new Promise<void>((resolve, reject) => {
    socket.addEventListener('open', () => resolve(), { once: true })
    socket.addEventListener('error', () => reject(new Error('relay refused the room')), { once: true })
  })
  return socket
}
