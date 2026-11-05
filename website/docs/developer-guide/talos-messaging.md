# Talos Messaging — the phone-first web app for hermes agents

Talos Messaging is a TypeScript PWA that talks to this machine's hermes agent
from an iPhone (or any phone) as if it were a chat app: a Telegram-shaped list
of conversations on the left, one thread of messages on the right, and nothing
in it but the agents this installation owns.

It is **not** the desktop Webapp (`gateway/webapp_listener.py`). That one is the
browser-hosted Desktop: terminal, files, settings, every dashboard route. Talos
Messaging is the small surface you add to a home screen and use with a thumb.
They share the gateway listener, the session API and the auth model; they do not
share a bundle.

## Why a separate app

- The dashboard bundle is ~700 routes of Desktop. Loading it on a phone is slow,
  and every extra route is another way for a stolen token to do something.
- A home-screen app has its own lifecycle (cold start, no tab bar, no refresh
  gesture on iOS). A PWA that assumes a phone is a different product decision
  from one that assumes a desktop.
- The install flow (a link carrying a key, emailed to the phone) has no
  counterpart on the desktop, where the browser is already on the same machine.

## Shape

```
apps/talos-messaging/            # Vite + React + TypeScript, npm workspace
  index.html                     # shell; boots from the URL fragment
  public/manifest.webmanifest    # add-to-home-screen metadata
  public/icons/                  # 180/192/512 + maskable
  src/main.tsx                   # token capture, router, theme
  src/transport.ts               # JSON-RPC over /api/ws, ticket auth
  src/store.ts                   # IndexedDB transcripts, localStorage prefs
  src/screens/{ThreadList,Thread,Compose}.tsx
  src/ui/                        # Telegram-shaped components
```

It is built by the same `scripts/build/web.mjs` path the dashboard uses and
served from the gateway listener at `/talos/`, so the phone and the agent are on
one origin and one port. `webapp.enabled` already exists; Talos Messaging needs
nothing new in the gateway's configuration beyond that.

## Transport

The app speaks the JSON-RPC dialect the Ink TUI and the dashboard's chat tab
already speak, over the same ticketed `/api/ws` socket
(`web/src/lib/gatewayClient.ts`, transport in `@hermes/shared`'s
`JsonRpcGatewayClient`):

- `session.create` → `{ session_id }` for a new conversation;
- `session.list` / the sessions router for the thread list, filtered to
  `Platform.API_SERVER` sessions so the app shows agents, never a gateway
  channel;
- `prompt.submit` `{ session_id, text }` to send;
- `message.delta` / `message.complete` events to render a streaming reply;
- `session.interrupt` for the stop button.

Auth is a ticket, not a long-lived query parameter: the app exchanges its bearer
token at `POST /api/webapp/socket-ticket` for a single-use, short-lived socket
ticket and passes that on the WebSocket URL. That is the same gate
`_ws_auth_ok` already enforces, so Talos Messaging needs no new auth path and
gains nothing a dashboard tab does not already have.

## Reaching a machine behind a NAT: the signaling relay

A phone on cellular cannot dial the agent's gateway: neither side has a routable
address. Talos Messaging therefore pairs over a WebRTC data channel, and the
**signaling relay** only introduces the two ends — it forwards SDP and ICE
between one agent socket and one phone socket and then steps out of the way.

- The relay lives at `https://clud.bot/talos-messaging/signal/{room}?role=agent|phone`
  (`clud.bot` is the public, WebSocket-capable host this project runs).
  `talosbot.ai` is GitHub Pages — static, `POST` answers 405 and a WebSocket
  upgrade is ignored — so the socket cannot terminate there; the static site
  publishes `https://talosbot.ai/talos-messaging/signal.json` naming the
  endpoint, and the app reads its endpoint from that document.
- The room is `sha256(install_key)[:32]`, so dialing the room proves knowing the
  key and the URL carries no credential of its own. The relay never sees the key.
- Frames are opaque and capped at 32 KiB; nothing is parsed, logged or stored; a
  room is reaped when both sides leave or after 300 idle seconds.
- The agent side dials the relay from `hermes talos-messaging serve` (the same
  key the install link carries), answers the offer, and exposes the gateway's
  JSON-RPC over the data channel — so the phone's transport stays
  `MessagingTransport`, with a WebRTC socket factory in place of `WebSocket`.

## The key and the install link

`hermes talos-messaging` (alias `hermes messaging`) is the command that puts the
app on a phone:

```
hermes talos-messaging [--print] [--email] [--revoke] [--url URL]
```

Implemented in `hermes_cli/talos_messaging.py`. Its tests live in
`tests/test_talos_messaging.py`.

1. **Mint or reuse a key.** A dedicated token — `webapp/messaging-token`,
   `0600`, 43 URL-safe characters, the same shape the Webapp session token uses
   — separate from the dashboard's own token so revoking one never signs the
   other out.
2. **Build the install link.** `https://<public_url>/talos/#k=<token>`. The key
   lives in the **fragment**, so it never reaches the server, its access log, or
   any proxy in front of it.
3. **Print it, and tell the user what to do with it.** The command prints the
   link plus the three steps below, and `--email` writes a self-contained HTML
   file (`~/talos-messaging-setup.html`) to send to the phone.
4. **`--revoke`** deletes the token and prints that the app will need a new link.

The key is never printed in full to a log or an email body the agent writes
later: the email carries the link, `--print` shows it, and nothing else does.

## Adding it to the home screen (the pictures)

The instructions live *outside* the app — they are what you read before the app
has ever opened. `--email` writes one HTML file with the images inlined as
base64 so it survives being mailed and opened from a phone:

| Step | Picture | What it shows |
| --- | --- | --- |
| 1 | `share-button.png` | Safari's share glyph in the toolbar |
| 2 | `add-to-home.png` | the share sheet with **Add to Home Screen** highlighted |
| 3 | `add-confirm.png` | the name field with **Talos Messaging** and the Add button |
| 4 | `open-app.png` | the new icon on the home screen |

The pictures are bundled assets (not screenshots of a specific iOS version's
chrome beyond the glyphs) and are generated from the same source SVGs that
produce the app icons, so a rebrand moves both at once.

## Persistent data

- **Transcripts** live in IndexedDB (`talos-messaging` database, one object
  store per conversation, keyed by message id), so a cold start on a phone with
  no network still shows the conversation.
- **Preferences** (last open thread, theme, font size) live in `localStorage`.
- The server remains the source of truth: on connect the app reconciles history
  from the sessions API and appends anything the store has that the server does
  not (a message queued while offline is resent once, marked as such).
- Nothing leaves the device: no analytics, no external font or script origins
  (`default-src 'self'`), and the token never appears in a URL the server sees.

## iOS specifics

- `display: standalone`, `apple-mobile-web-app-capable`, a 180×180
  `apple-touch-icon`, and a `maskable` 512 icon.
- `viewport-fit=cover` plus `env(safe-area-inset-bottom)` on the composer, so
  the send button is not under the home indicator.
- Text size honours Dynamic Type; the layout holds at 320 pt and at landscape.
- No push notifications in v1: iOS only delivers web push to an installed PWA
  after a user gesture, and a chat that silently drops messages is worse than
  one that polls when foregrounded.

## Testing

- `vitest` for the store (reconcile, offline queue, eviction) and the transport
  (ticket exchange, reconnect, event mapping) against a fake socket.
- `pytest` for `hermes talos-messaging`: token minting is idempotent, `--revoke`
  removes it, the link puts the key in the fragment and never in the path or
  query, the email file contains the link and the four images, and no code path
  writes the token to a log.
- The existing docker/Playwright harness gets one flow: open the install link on
  a phone-sized viewport, send a prompt, see a reply, reload and still see it.
