/**
 * Talos Messaging: a Telegram-shaped surface over this installation's agent
 * conversations and nothing else. No channels, no contacts, no settings beyond
 * the thread you were reading.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { MessagingTransport, type AgentThread } from './transport'
import { localMessageId, readPreference, reconcile, TranscriptStore, writePreference, type Message } from './store'
import { keyFingerprint } from './auth'

interface Props {
  token: string
}

export function App({ token }: Props): React.ReactElement {
  const transport = useMemo(() => new MessagingTransport(token), [token])
  // The phone goes into a tunnel; the thread you were reading must survive it.
  const store = useMemo(() => new TranscriptStore(), [])
  const [threads, setThreads] = useState<AgentThread[]>([])
  const [active, setActive] = useState<string | null>(() => readPreference(window.localStorage, 'thread'))
  const [messages, setMessages] = useState<Message[]>([])
  const [draft, setDraft] = useState('')
  const [status, setStatus] = useState<'connecting' | 'ready' | 'offline' | 'denied'>('connecting')
  const streaming = useRef<Map<string, string>>(new Map())

  useEffect(() => {
    let cancelled = false
    void (async () => {
      try {
        await transport.connect()
        if (cancelled) return
        setStatus('ready')
        setThreads(await transport.threads())
      } catch (error) {
        if (cancelled) return
        setStatus(error instanceof Error && error.message === 'key rejected' ? 'denied' : 'offline')
      }
    })()
    const stop = transport.onDelta((reply) => {
      const next = `${streaming.current.get(reply.sessionId) ?? ''}${reply.text}`
      streaming.current.set(reply.sessionId, next)
      setMessages((current) => {
        const id = `stream-${reply.sessionId}`
        const others = current.filter((message) => message.id !== id)
        return reconcile(others, [
          { id, sessionId: reply.sessionId, role: 'agent', text: next, at: new Date().toISOString() },
        ])
      })
    })
    return () => {
      cancelled = true
      stop()
    }
  }, [transport])

  useEffect(() => {
    if (active) writePreference(window.localStorage, 'thread', active)
    if (!active) return
    let cancelled = false
    void (async () => {
      try {
        const stored = await store.messages(active)
        if (!cancelled && stored.length > 0) setMessages((current) => reconcile(current, stored))
      } catch {
        // No IndexedDB (private mode, quota): the thread still works, it just
        // will not survive the next cold start.
      }
    })()
    return () => {
      cancelled = true
    }
  }, [active, store])

  // Persist the reply the moment the agent finishes it (one write per turn, not
  // one per delta).
  useEffect(() => {
    const stop = transport.onComplete((sessionId) => {
      const text = streaming.current.get(sessionId) ?? ''
      streaming.current.delete(sessionId)
      if (!text) return
      const message: Message = {
        id: `srv-${sessionId}-${Date.now()}`,
        sessionId,
        role: 'agent',
        text,
        at: new Date().toISOString(),
      }
      void store.append(message).catch(() => undefined)
      setMessages((current) => current.filter((entry) => entry.id !== `stream-${sessionId}`).concat(message))
    })
    return stop
  }, [store, transport])

  const startThread = useCallback(async () => {
    try {
      const id = await transport.createThread()
      setThreads((current) => [{ id, title: 'New agent', updatedAt: new Date().toISOString(), preview: '' }, ...current])
      setActive(id)
      setMessages([])
    } catch {
      setStatus('offline')
    }
  }, [transport])

  const send = useCallback(async () => {
    const text = draft.trim()
    if (!text || !active) return
    const message: Message = {
      id: localMessageId(),
      sessionId: active,
      role: 'user',
      text,
      at: new Date().toISOString(),
      pending: true,
    }
    setMessages((current) => [...current, message])
    void store.append(message).catch(() => undefined)
    setDraft('')
    streaming.current.set(active, '')
    try {
      await transport.send(active, text)
    } catch {
      setStatus('offline')
    }
  }, [active, draft, transport])

  const thread = threads.find((candidate) => candidate.id === active) ?? null

  return (
    <div className="app">
      <header className="bar">
        <button className="icon" onClick={() => setActive(null)} aria-label="Conversations" hidden={active === null}>
          ‹
        </button>
        <div className="who">
          <strong>{thread?.title ?? 'Talos Messaging'}</strong>
          <span className={`dot ${status}`} title={status} />
          <small>{keyFingerprint(token)}</small>
        </div>
        <button className="icon" onClick={() => void startThread()} aria-label="New conversation">
          +
        </button>
      </header>

      {active === null ? (
        <ul className="threads">
          {threads.length === 0 && status === 'ready' ? (
            <li className="empty">No conversations yet. Tap + to start one.</li>
          ) : null}
          {threads.map((candidate) => (
            <li key={candidate.id}>
              <button className="thread" onClick={() => setActive(candidate.id)}>
                <span className="avatar" aria-hidden="true" />
                <span className="meta">
                  <strong>{candidate.title}</strong>
                  <small>{candidate.preview || 'Tap to open'}</small>
                </span>
                <time>{new Date(candidate.updatedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <>
          <ol className="messages">
            {messages.map((message) => (
              <li key={message.id} className={message.role}>
                <p>{message.text}</p>
              </li>
            ))}
          </ol>
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault()
              void send()
            }}
          >
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Message"
              rows={1}
              aria-label="Message"
            />
            <button type="submit" disabled={!draft.trim()} aria-label="Send">
              ↑
            </button>
          </form>
        </>
      )}

      {status === 'denied' ? (
        <p className="banner">This key was revoked. Run <code>hermes talos-messaging --print</code> for a new link.</p>
      ) : null}
      {status === 'offline' ? <p className="banner">Offline. Messages are kept and sent when the agent is back.</p> : null}
    </div>
  )
}
