/**
 * The wire: the same JSON-RPC the TUI and the dashboard's chat tab speak, over
 * the gateway's ticketed `/api/ws` socket.
 *
 * The install key is a bearer credential, never a query parameter the server
 * sees: it is exchanged for a single-use socket ticket on this origin
 * (`POST /api/webapp/socket-ticket?socket=gateway`) and the ticket goes on the
 * WebSocket URL. A reconnect mints a new ticket; a revoked key fails here.
 */

import { JsonRpcGatewayClient, buildHermesWebSocketUrl } from '@hermes/shared'
import { openRelaySocket } from './relay'

export interface AgentThread {
  id: string
  title: string
  updatedAt: string
  preview: string
}

export interface Reply {
  sessionId: string
  text: string
}

const TICKET_PATH = '/api/webapp/socket-ticket?socket=gateway'

export class MessagingTransport extends JsonRpcGatewayClient {
  /** The relay's seat, when the gateway's own address is not reachable. */
  private relaySocket: WebSocket | null = null
  private readonly directUrl: string

  constructor(private readonly token: string) {
    // A socket factory lets the same client speak to whichever path opened:
    // the gateway's own `/api/ws`, or a relay seat that a bridge forwards.
    super({
      closedErrorMessage: 'Connection closed',
      connectErrorMessage: 'Cannot reach the agent',
      notConnectedErrorMessage: 'Not connected',
      requestIdPrefix: 'tm',
      socketFactory: (url) => this.relaySocket ?? new WebSocket(url),
    })
    this.directUrl = '/api/ws'
  }

  /**
   * Reach the agent by the shortest path that works.
   *
   * Same origin first — the phone on the same network, or through a tunnel,
   * dials the gateway directly with a single-use ticket. When that address does
   * not answer, the phone dials the public relay instead and
   * `hermes talos-messaging serve` carries the frames to the gateway. A rejected
   * key is never retried over the relay: the key is the problem, not the route.
   */
  async connect(): Promise<void> {
    try {
      const ticket = await fetchTicket(this.token)
      await super.connect(
        buildHermesWebSocketUrl({ authParam: ['ticket', ticket], basePath: '', path: this.directUrl }),
      )
      return
    } catch (error) {
      if (isKeyRejection(error)) throw error
      // No gateway here: meet the agent at the relay.
    }
    this.relaySocket = await openRelaySocket(this.token)
    // The factory above returns the relay seat, so the URL is only a placeholder.
    await super.connect(
      buildHermesWebSocketUrl({ authParam: ['ticket', 'relay'], basePath: '', path: this.directUrl }),
    )
  }

  /** Ask the gateway which agent conversations this installation has. */
  async threads(): Promise<AgentThread[]> {
    const result = await this.request<{ sessions?: Array<Record<string, unknown>> }>('session.list', {})
    const sessions = result?.sessions ?? []
    return sessions
      .filter((session) => session.source === 'api_server' || session.platform === 'api_server')
      .map((session) => ({
        id: String(session.session_id ?? session.id ?? ''),
        title: String(session.title ?? session.name ?? 'Agent'),
        updatedAt: String(session.updated_at ?? session.last_active ?? new Date().toISOString()),
        preview: String(session.preview ?? ''),
      }))
      .filter((thread) => thread.id !== '')
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
  }

  /** Start a conversation and return its id. */
  async createThread(): Promise<string> {
    const result = await this.request<{ session_id: string }>('session.create', {})
    return result.session_id
  }

  /** Send one user message; deltas arrive through `onDelta`. */
  async send(sessionId: string, text: string): Promise<void> {
    await this.request('prompt.submit', { session_id: sessionId, text })
  }

  /** Stop the reply that is streaming. */
  async interrupt(sessionId: string): Promise<void> {
    await this.request('session.interrupt', { session_id: sessionId })
  }

  /** The agent finished a reply for one conversation. */
  onComplete(handler: (sessionId: string) => void): () => void {
    return this.on('message.complete', (event) => {
      const payload = (event.payload ?? {}) as { session_id?: string }
      if (payload.session_id) handler(payload.session_id)
    })
  }

  /** Streaming text for one conversation. */
  onDelta(handler: (reply: Reply) => void): () => void {
    return this.on('message.delta', (event) => {
      const payload = (event.payload ?? {}) as { session_id?: string; text?: string }
      if (!payload.session_id || typeof payload.text !== 'string') return
      handler({ sessionId: payload.session_id, text: payload.text })
    })
  }
}

/** A rejected key is the end of the road; only an unreachable gateway falls back. */
export function isKeyRejection(error: unknown): boolean {
  return error instanceof Error && error.message === 'key rejected'
}

/**
 * Mint a single-use socket ticket from the install key.
 *
 * The key rides in its own header: the server accepts it on this one route and
 * only for `socket=gateway`, so a phone can open a chat socket and nothing else
 * — never a shell, a file or a dashboard page.
 */
export async function fetchTicket(token: string): Promise<string> {
  const response = await fetch(TICKET_PATH, {
    method: 'POST',
    headers: { 'X-Talos-Messaging-Key': token },
  })
  if (!response.ok) {
    throw new Error(response.status === 401 || response.status === 403 ? 'key rejected' : `ticket ${response.status}`)
  }
  const body = (await response.json()) as { ticket?: string }
  if (!body.ticket) throw new Error('no ticket in response')
  return body.ticket
}
