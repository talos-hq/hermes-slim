# Talos mobile messaging

Talos is an outbound notification platform plugin. Like Discord and Email it exports
`register(ctx)`, registers a `BasePlatformAdapter` factory with `ctx.register_platform`,
and returns `SendResult` from async sends. It does not subscribe to logs, tool hooks,
LLM deltas, or conversation transcripts. The gateway separately hosts the authoritative mailbox
and Web Push receiver described below.

## Message path and deliberate emission

An agent integration explicitly calls
`await adapter.send_notification(user_id, session_id, message)` with this closed envelope:

```json
{"id":"durable-event-id","origin":"agent","intent":"notify","channel":"final","kind":"text","text":"Your report is ready."}
```

The producer must allocate and persist the event ID once, before its first attempt.
Reuse it for retries and crash recovery; use a fresh ID for a new notification,
even when the text is identical. IDs must never be reused for different content.
The adapter's normal `send(chat_id, content, metadata=...)` requires exactly
`{"session_id": "...", "notification": <envelope>}` and matching content.
Plain replies, progress updates and implicit gateway output fail closed.

The connector rejects unknown envelope fields, non-agent origins, non-final channels,
non-notification intents, structured content, serialized JSON blocks, fenced blocks,
reasoning/tool tags, tool-call fields and log prefixes. It rejects the entire message,
including suspicious content wrapped inside an otherwise valid envelope; it never
strips a wrapper and forwards a remainder. This is a provenance contract plus a
conservative content filter, not a semantic classifier. A producer must never relabel
or paraphrase private tool output/reasoning as deliberate final text. Arbitrary natural
language cannot prove its origin; this connector does not claim that it can.

## Configuration and manual trigger

Enable `talos-platform` through the existing plugin manager. Configure the platform's
`extra.endpoint` as the full HTTPS mailbox write URL in gateway platform config
(`platforms.talos.extra.endpoint`); `extra.dry_run: true` disables network writes.
Store `TALOS_API_TOKEN` in the owning profile's secret store, never YAML or source.
The adapter reads it through `agent.secret_scope.get_secret`.

The plugin registers a manual command using the existing CLI extension surface:

```sh
hermes talos --endpoint https://mailbox.example/notifications \
  --user-id USER --session-id SESSION --message-id UNIQUE_EVENT_ID \
  --message 'Your report is ready.' --dry-run
```

Remove `--dry-run` to submit. Dry-run validates and computes the key but creates no
HTTP client and performs no request. It prints status and the key, not the token or
message body. This command is an explicit operator notification, not transcript export.

The HTTP contract is `POST <endpoint>` with Bearer authorization and
`Idempotency-Key: sha256(JSON([user_id, session_id, event_id]))`. The JSON body contains
`user_id`, `session_id`, `message_id` (event ID), and `message` (plain text).
The mailbox server must authenticate and authorize the recipient/session, enforce
atomic uniqueness on the idempotency key, reject conflicting reuse, and persist the
message before acknowledging it. Client retries alone cannot provide deduplication.

The client makes at most three attempts (hard cap five), with exponential backoff
starting at 250 ms and a ten-second HTTP timeout per attempt. Only transport errors,
429 and 5xx retry. 401/403 and other 4xx stop immediately. Redirects are never followed. Plain HTTP is accepted only for literal loopback
addresses used in local development; remote endpoints require HTTPS.
All attempts carry the same key; exhausted sends return failure without requesting
another base-adapter retry loop. There is no durable local outbox: the producer owns
recovery and must retain the original event ID.

## Gateway mailbox and Web Push

The gateway exposes these routes on its existing authenticated webapp API. They use the
same browser cookie or loopback session token as `/api/webapp/socket-ticket`; socket tickets
themselves are single-use socket credentials and cannot authorize REST requests.

| Method and path | Request / response |
| --- | --- |
| `GET /api/push/vapid-public-key` | `{ "public_key": "<base64url P-256 public key>" }` |
| `POST /api/push/subscribe` | `{ "device_id": "iphone", "subscription": <PushSubscription.toJSON()> }` |
| `DELETE /api/push/subscribe?device_id=iphone` | Revoke that user's device; safe to repeat |
| `POST /api/push/mailbox` | `{ "message_id": "event-id", "session_id": "session-id", "message": "Final text" }` |
| `GET /api/push/mailbox?after=0` | `messages` (up to 100), `next_cursor`; use the cursor as `after` |
| `PATCH /api/push/mailbox/{message_id}/read` | Persist read state |

Enrollment replaces the subscription for the authenticated user/device pair. The mailbox writer
can specify `user_id` for compatibility with the Talos connector; it must match the authenticated
principal (`legacy-token-owner` for the loopback browser token). Configure the connector endpoint
as `https://<gateway>/api/push/mailbox` and its token as the existing authorized gateway credential.
This endpoint cannot send to an arbitrary different user's mailbox.

Set `push.subject` in `config.yaml` to your VAPID contact URI, for example `mailto:ops@example.com`.
Store `TALOS_VAPID_PRIVATE_KEY` only in the server environment/profile secret store, as base64url
raw P-256 or base64 DER private key material. No key is generated implicitly. The public-key
route derives the public point; the generic secret-reveal route refuses this server-only key.
Push is disabled without a key and subject, while mailbox writes and reads continue to work.

Subscriptions and mailbox rows persist in the profile's existing `state.db`. Identity includes
provider, issuer and user ID. Each mailbox row contains an ID, UTC timestamp, session ID, message,
read flag and monotonic cursor. An atomic `(user, message_id)` uniqueness check commits history
before network delivery; identical repeats return `created: false`, conflicting reuse returns 409.
Concurrent requests and restarts cannot start a second push batch for that message.

Wake-ups contain only `{ "message_id": "event-id" }`, encrypted with `aes128gcm` and authenticated
using VAPID. Each enrolled device has at most three attempts with exponential backoff and a
ten-second timeout. Transport failures, 429 and 5xx retry; other 4xx and redirects never do.
404/410 revoke only the failed subscription generation, preserving any concurrent replacement.
A crash after the mailbox commit can lose a wake-up. An uncertain network response may cause
duplicate delivery at the push provider within the retry budget; Web Push has no exactly-once
receipt. The durable mailbox remains authoritative and no replay starts another delivery batch.

## Mailbox, push and device boundary

The authenticated mailbox is authoritative. A mailbox write may cause the server to
send Web Push as a wake-up hint. Push carries no authoritative message history;
clients fetch the mailbox using their own authenticated device session and reconcile
by message ID/cursor. Missed, duplicated or reordered wake-ups do not change mailbox
state. Push acceptance and mailbox acceptance are separate receipts; neither means
the user read a message.

A future watchOS client is a separate device boundary with its own enrollment,
credentials, authorization, revocation and mailbox synchronization. An iPhone/browser
subscription does not grant a watch access. Mirrored watch notifications are display
surfaces, not an agent session, credential delegation, microphone API or transport.
Voice capture would require a foreground, user-initiated experience with platform
permissions and a separately designed authenticated upload path.

VAPID private keys belong only to the push service's server-side secret store, with
restricted service access, backup/rotation procedures and no logging. Only the VAPID
public key goes to subscribing clients. Neither this connector nor browser/watch
bundles hold the private key. Mailbox write tokens and device credentials remain
separate from VAPID signing authority. The connector plugin sends no Web Push itself.

## What we do NOT claim

- No silent push or guaranteed background execution.
- No background microphone capture.
- No microphone access via mirrored watch notifications.
- No delivery guarantee from a 2xx push-service response.
- No guarantee that mailbox acceptance means device receipt, display or reading.
- No exactly-once delivery without server-side atomic idempotency enforcement.
- No implemented watchOS client or durable connector-side outbox.
- No semantic proof that arbitrary text was never derived from private content.

## Voice, later — designed here, deliberately not built

The pilot ships text only. This section fixes the seams so adding voice does not
mean rebuilding the transport, the mailbox, or the Hermes integration.

### Message model

The mailbox record gains two fields and one new content kind; nothing else changes:

| field | meaning |
| --- | --- |
| `kind` | `text` (today) or `audio` |
| `audio_ref` | opaque server-side blob reference; never a public URL |
| `transcript` | `pending` / `ready` / `failed`, plus text when ready |
| `session_id` | unchanged — the reply must land in the conversation that asked |

A voice reply is a mailbox row with `kind=audio` pointing at a blob. The device
fetches the blob through the authenticated API, so access control stays the same
as text and a leaked `audio_ref` is useless on its own.

### Client capture

- `navigator.mediaDevices.getUserMedia({audio:true})` **only** from a user gesture
  (a tap). Never on load, never from the service worker.
- Record with `MediaRecorder`, negotiating `mimeType`: Safari offers
  `audio/mp4`, Chromium `audio/webm;codecs=opus`. Feature-detect with
  `MediaRecorder.isTypeSupported`, and send the chosen type to the server rather
  than assuming a container.
- State machine, explicit and cancellable:
  `idle → requesting-permission → recording → uploading → transcribing → sent`,
  with `failed` reachable from every state and `cancel` tearing down the stream
  in all of them. A stuck `recording` state that keeps the mic light on is a
  privacy bug, not a UX bug.
- Upload with an idempotency key so a retry cannot create two messages.
- Offline: keep the blob locally, show it as `queued`, upload on reconnect.

### Transcription — server-side by default

Do **not** rely on in-browser `SpeechRecognition`. On WebKit it is
vendor-prefixed, capped around 60 seconds of audio, unreliable after other audio
playback has run, and unavailable in several contexts; the failure mode is a
silent empty transcript. Instead:

1. Client uploads the blob: `POST /api/voice` (multipart, authenticated, idempotent).
2. Server stores the blob, creates the mailbox row with `transcript=pending`, and
   enqueues transcription.
3. Transcription runs server-side (a hosted STT provider, or a local model behind
   the same interface). On success the row becomes `transcript=ready` with text
   and the original Hermes session receives the reply as a normal user message —
   identical to the text path, which is the whole point.
4. On failure the row becomes `transcript=failed`, **the audio is kept**, and the
   UI offers retry or playback. Losing the recording because the transcriber was
   down is unacceptable.

The adapter interface is deliberately narrow — `transcribe(blob, mime) -> text` —
so a local model can replace a hosted provider without touching the mailbox, the
connector, or the client.

### Text-to-speech

- Synthesise on demand: `GET /api/messages/{id}/audio`, cached by content hash so
  repeated plays don't re-bill synthesis.
- **Play is a user-initiated control.** iOS blocks or silently swallows autoplay;
  treat audible autoplay as an experiment, never as the designed path.
- Playback uses a single `HTMLAudioElement` with explicit `stop`, so two messages
  can't talk over each other; `visibilitychange` pauses it.
- A push payload still carries only an id and a short line. Audio is fetched after
  the user acts, so a lock screen never holds agent content.

### What voice will still not do

- No microphone from the service worker or from a push wake-up, on any platform.
- No autoplay of speech on iOS as a guaranteed behaviour.
- No watch microphone or dictation without a native watchOS companion; a mirrored
  notification carries no microphone permission and no reply affordance.
- No background recording; the mic is live only while the user is in a recording
  state with the app foregrounded.
