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
  private ticketSource: () => Promise<string>

  constructor(token: string) {
    super({
      closedErrorMessage: 'Connection closed',
      connectErrorMessage: 'Cannot reach the agent',
      notConnectedErrorMessage: 'Not connected',
      requestIdPrefix: 'tm',
    })
    this.ticketSource = () => fetchTicket(token)
  }

  /** Dial `/api/ws` with a fresh ticket minted from the install key. */
  async connect(): Promise<void> {
    const ticket = await this.ticketSource()
    await super.connect(
      buildHermesWebSocketUrl({ authParam: ['ticket', ticket], basePath: '', path: '/api/ws' }),
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

  /** Streaming text for one conversation. */
  onDelta(handler: (reply: Reply) => void): () => void {
    return this.on('message.delta', (event) => {
      const payload = (event.payload ?? {}) as { session_id?: string; text?: string }
      if (!payload.session_id || typeof payload.text !== 'string') return
      handler({ sessionId: payload.session_id, text: payload.text })
    })
  }
}

/** Mint a single-use socket ticket from the install key. */
export async function fetchTicket(token: string): Promise<string> {
  const response = await fetch(TICKET_PATH, {
    method: 'POST',
    headers: { 'X-Hermes-Session-Token': token },
    credentials: 'include',
  })
  if (!response.ok) {
    throw new Error(response.status === 401 || response.status === 403 ? 'key rejected' : `ticket ${response.status}`)
  }
  const body = (await response.json()) as { ticket?: string }
  if (!body.ticket) throw new Error('no ticket in response')
  return body.ticket
}
