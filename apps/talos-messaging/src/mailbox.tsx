import { useEffect, useState } from 'react'
import { api, shouldRefreshMailbox } from './push'

interface MailboxMessage { id: string; message: string }

export function Mailbox({ token }: { token: string }): React.ReactElement {
  const [messages, setMessages] = useState<MailboxMessage[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    let disposed = false
    let previous = document.visibilityState
    async function refresh() {
      try {
        let path = '/api/mailbox'
        let cursor = 0
        const collected: MailboxMessage[] = []
        while (!disposed) {
          let response = await fetch(`${path}?after=${cursor}`, { headers: { 'X-Talos-Messaging-Key': token } })
          if (response.status === 404 && path === '/api/mailbox') {
            path = '/api/push/mailbox'
            response = await api(token, `${path}?after=${cursor}`)
          }
          if (!response.ok) throw new Error('Mailbox unavailable')
          const body = await response.json()
          collected.push(...body.messages)
          if (!body.messages.length || !(body.next_cursor > cursor)) break
          cursor = body.next_cursor
        }
        if (!disposed) { setMessages(collected); setError('') }
      } catch { if (!disposed) setError('Mailbox unavailable. Return to the app to retry.') }
    }
    function visibilityChanged() {
      const current = document.visibilityState
      if (shouldRefreshMailbox(previous, current)) void refresh()
      previous = current
    }
    void refresh()
    document.addEventListener('visibilitychange', visibilityChanged)
    return () => { disposed = true; document.removeEventListener('visibilitychange', visibilityChanged) }
  }, [token])
  return <section className="mailbox" aria-label="Mailbox">
    {error && <p role="status">{error}</p>}
    {messages.map((message) => <p key={message.id}>{message.message}</p>)}
  </section>
}
