# Talos mobile messaging

Talos is an outbound notification platform plugin. Like Discord and Email it exports
`register(ctx)`, registers a `BasePlatformAdapter` factory with `ctx.register_platform`,
and returns `SendResult` from async sends. It does not subscribe to logs, tool hooks,
LLM deltas, or conversation transcripts. No core files are changed.

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
separate from VAPID signing authority. This plugin sends no Web Push itself.

## What we do NOT claim

- No silent push or guaranteed background execution.
- No background microphone capture.
- No microphone access via mirrored watch notifications.
- No delivery guarantee from a 2xx push-service response.
- No guarantee that mailbox acceptance means device receipt, display or reading.
- No exactly-once delivery without server-side atomic idempotency enforcement.
- No implemented watchOS client, push service, mailbox server or durable local outbox.
- No semantic proof that arbitrary text was never derived from private content.
