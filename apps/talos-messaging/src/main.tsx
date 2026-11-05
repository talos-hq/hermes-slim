/**
 * Boot: take the key off the install link, scrub it from the address bar, and
 * render. A browser with no key shows the "open your install link" state
 * instead of an empty chat list, because an empty list looks like a working
 * app that lost its data.
 */

import { StrictMode, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import './style.css'
import { App } from './app'
import { captureKey, stripFragment, type CapturedKey } from './auth'

function Root(): React.ReactElement {
  const [key, setKey] = useState<CapturedKey | null>(null)
  const [checked, setChecked] = useState(false)

  useEffect(() => {
    const captured = captureKey(window.location.href, window.localStorage)
    if (captured?.source === 'fragment') {
      window.history.replaceState(null, '', stripFragment(window.location.href))
    }
    setKey(captured)
    setChecked(true)
  }, [])

  if (!checked) return <main className="boot">Loading…</main>
  if (!key) {
    return (
      <main className="boot">
        <h1>Talos Messaging</h1>
        <p>Open the install link hermes gave you on this device, then add this page to your home screen.</p>
      </main>
    )
  }
  return <App token={key.token} />
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
