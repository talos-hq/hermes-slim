---
sidebar_position: 20
title: "Talos Messaging (phone app)"
description: "Chat with your agents from an iPhone home screen — the key, the link, and the pictures"
---

# Talos Messaging

Talos Messaging is a small web app you add to your phone's home screen and use
like a chat app: your agents, their replies, and nothing else — no bot channels,
no settings, no terminal.

It is a separate thing from the desktop dashboard. Same machine, same agents,
different surface.

## What you get

- **A home-screen app.** Added from Safari, it opens full-screen with no address
  bar, its own icon, and keeps its state.
- **Only your agents.** The list shows conversations with this installation's
  agents; a messaging-platform channel never appears.
- **History that survives a cold start.** Messages are stored on the phone, so
  opening the app in a tunnel still shows what you were reading.
- **A link that signs you in.** No password to type on a phone keyboard: the
  install link carries the key.

## Put it on the phone

```bash
hermes talos-messaging --email
```

That mints a key (once), prints the install link, and writes
`~/talos-messaging-setup.html` — a single page with the link and the four
Add-to-Home-Screen steps, each with a picture. Send that file to yourself (mail,
AirDrop, Notes) and open it **on the phone**. It is deliberately separate from
the app: you read it before the app has ever run.

Then:

1. Open the link on the iPhone. Safari shows Talos Messaging.
2. Tap **Share** (the square with the arrow).
3. Choose **Add to Home Screen**.
4. Tap **Add**.

The icon appears on the home screen and opens like an app from then on.

## The key

- It lives in `~/.hermes/webapp/messaging-token`, owner-only.
- It is **not** your dashboard password. It can open one thing: a chat socket. It
  cannot open a shell, read a file, or change a setting.
- The link carries it in the URL *fragment* (`…/talos/#k=…`), which browsers
  never send to a server — so no access log, proxy or analytics ever sees it. The
  app reads it once and clears it from the address bar.
- Anyone with the link can talk to your agents. Send it to yourself, not to a
  group.

Rotate it any time:

```bash
hermes talos-messaging --revoke     # the phone's app then asks for a new link
hermes talos-messaging --print      # just the link, e.g. for a QR code
```

## Reaching the machine from anywhere

If the phone and the machine are on the same network (or you already have a
tunnel to the dashboard), the app talks to the gateway directly.

From cellular, they cannot dial each other. Run:

```bash
hermes talos-messaging serve
```

Both ends then meet at the public relay
(`clud.bot/talos-messaging/signal`, published at
`talosbot.ai/talos-messaging/signal.json`), and that command carries the frames
to your local gateway. It prints when the phone arrives; Ctrl+C stops it. The
relay only forwards encrypted-in-transit JSON between the two seats, keeps
nothing, and the room's name is derived from your key, so the relay never holds
the key itself.

> **Status:** the relay endpoints above are **not deployed yet** — verified
> 2026-11-04, `clud.bot/talos-messaging/` and
> `talosbot.ai/talos-messaging/signal.json` both return 404. Until they exist,
> the phone and the gateway must be on the same network. Web Push delivery also
> requires the app to be served from a public HTTPS origin, which is a
> deployment step, not a configuration flag.

## Troubleshooting

| What you see | What it means |
| --- | --- |
| "Open the install link hermes gave you" | This browser has no key. Open the link from `hermes talos-messaging --print`. |
| "This key was revoked" | Run `hermes talos-messaging --revoke` then mint a new link and re-add the app. |
| "Offline. Messages are kept…" | The gateway is not reachable. Start `hermes talos-messaging serve`, or get back on the network. |
| A screenshot instead of an icon | The app was added to the home screen before its icons existed. Remove and re-add it. |

## Apple Watch

The watch mirrors the phone's notifications. There is no watch app, and nothing
on the watch talks to the gateway directly.

Settings, in this order:

1. **iPhone → Watch app → Notifications** → enable **Mirror my iPhone**. If it is
   off, the watch shows only a subset of alerts.
2. **iPhone → Settings → Notifications → Talos Messaging** → allow
   notifications, and keep **Lock Screen** and **Banners** on. With notifications
   off for the app, the watch has nothing to mirror.
3. **iPhone → Watch app → Notifications → Talos Messaging** → confirm it is not
   set to **Deliver Quietly**, which suppresses the banner on the wrist.
4. **Focus / Do Not Disturb** on either device suppresses delivery. Check the
   Focus state before concluding the pipeline is broken.

What the watch **does not** give you: the watch shows the mirrored notification
but cannot open a microphone, dictate a reply into the conversation, or press
approve/retry. That needs a native watchOS companion app. A mirrored alert is
also not independent of the iPhone — the watch will not receive it when the
phone is unlocked and in use, out of range, or with notifications disabled for
the app.

## Acceptance checklist (real iPhone)

Nothing below can be proven from a test suite. A `2xx` from the push service is
**not** delivery to a device — only this list counts. Record the result of each
step; on a failure, note which of the five stages (subscribe, store, send,
deliver, display) stopped.

| # | Step | Pass looks like |
| --- | --- | --- |
| 1 | Open the install link in Safari, **Share → Add to Home Screen**, launch from the icon | Standalone window, no Safari chrome |
| 2 | Tap **Enable notifications** (a user gesture; it must not fire on load) | iOS permission prompt appears |
| 3 | Confirm the subscription reached the server | The `POST /api/push/subscribe` response was 2xx and the device appears in the gateway's push store |
| 4 | Trigger a real message from the agent, not `curl` against a stub | Mailbox row created with the message id |
| 5 | Receive it with the app open | In-app message appears |
| 6 | Receive it with the app **closed** (swipe it away, lock the phone) | Notification banner on the lock screen |
| 7 | Tap the notification | App opens on that message, not the inbox |
| 8 | Read the mailbox while the phone was offline | Messages sent during the outage are present |
| 9 | Revoke the key (`hermes talos-messaging --revoke`), then send | Server drops the subscription on 404/410; no silent failure |
| 10 | Watch, with Mirror my iPhone on | The same alert appears on the wrist |

Steps 1–3 are reachable on a local network. Steps 4–10 require a public HTTPS
origin for the app plus the deployment in
[Deployment](../deployment/talos-messaging.md).
