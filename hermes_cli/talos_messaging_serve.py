"""``hermes talos-messaging serve`` — carry the phone's chat through a NAT.

The phone and this machine are usually on different networks, so neither can
dial the other. Both dial *out* to the public relay instead, and the relay
introduces them (``clud.bot/talos-messaging/signal/{room}``; the address is
published at ``talosbot.ai/talos-messaging/signal.json``).

This process is the agent's end of that bridge:

    phone ⇄ relay (opaque JSON-RPC frames) ⇄ this bridge ⇄ local gateway /api/ws

- The room is ``sha256(messaging_key)[:32]``, so only someone holding this
  installation's key can meet the bridge there.
- The local gateway socket is authenticated with a ticket minted from that same
  key (``POST /api/webapp/socket-ticket?socket=gateway``), so the bridge holds no
  credential the phone does not already have.
- Frames are forwarded verbatim in both directions; this process parses none of
  them and logs none of their contents.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from typing import Any

import httpx
import websockets

#: Where the relay's own description is published.
SIGNAL_DOC_URL = "https://talosbot.ai/talos-messaging/signal.json"
#: Used when the document cannot be fetched.
FALLBACK_RELAY = "ws://127.0.0.1:8124/talos-messaging/signal"
#: Relay control frames (never forwarded to the gateway).
CONTROL_TYPES = frozenset({"peer_joined", "peer_left"})
RECONNECT_SECONDS = (1, 2, 5, 10, 30)


def room_for(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def relay_socket_url(base: str, room: str, role: str = "agent") -> str:
    return f"{base.rstrip('/')}/{room}?role={role}"


async def resolve_relay(explicit: str | None, *, doc_url: str = SIGNAL_DOC_URL, timeout: float = 10.0) -> str:
    """``--relay``, else the published address, else the loopback default."""
    if explicit:
        return explicit
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(doc_url)
            response.raise_for_status()
            template = str(response.json().get("websocket") or "")
    except Exception:
        return FALLBACK_RELAY
    # The document names a template: ".../{room}?role=agent|phone".
    base = template.split("{room}", 1)[0].rstrip("/")
    return base or FALLBACK_RELAY


async def mint_gateway_ticket(gateway: str, key: str, *, timeout: float = 10.0) -> str:
    """A one-use ticket for the gateway's chat socket, minted from the phone key."""
    url = f"{gateway.rstrip('/')}/api/webapp/socket-ticket?socket=gateway"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers={"X-Talos-Messaging-Key": key})
        response.raise_for_status()
        ticket = response.json().get("ticket")
    if not ticket:
        raise RuntimeError("the gateway returned no ticket")
    return str(ticket)


def gateway_socket_url(gateway: str, ticket: str) -> str:
    base = gateway.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    return f"{base}/api/ws?ticket={ticket}"


def is_control(frame: str) -> bool:
    """Relay housekeeping (peer presence), not something the gateway should see."""
    try:
        payload = json.loads(frame)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("type") in CONTROL_TYPES


async def bridge(
    relay_url: str,
    room: str,
    gateway: str,
    key: str,
    *,
    log,
    stop: asyncio.Event | None = None,
) -> None:
    """Serve one room until *stop* is set, reconnecting on failure."""
    attempt = 0
    while stop is None or not stop.is_set():
        try:
            await _bridge_once(relay_url, room, gateway, key, log=log, stop=stop)
            attempt = 0
        except asyncio.CancelledError:
            raise
        except Exception as error:  # a dropped relay must not end the command
            delay = RECONNECT_SECONDS[min(attempt, len(RECONNECT_SECONDS) - 1)]
            log(f"bridge: {type(error).__name__}: {error} (retrying in {delay}s)")
            attempt += 1
            if stop is not None:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
                continue
            await asyncio.sleep(delay)


async def _bridge_once(
    relay_url: str,
    room: str,
    gateway: str,
    key: str,
    *,
    log,
    stop: asyncio.Event | None,
) -> None:
    url = relay_socket_url(relay_url, room)
    async with websockets.connect(url, ping_interval=20, ping_timeout=20) as relay:
        log("bridge: joined the room; waiting for the phone")
        upstream: Any = None
        try:
            while True:
                if stop is not None and stop.is_set():
                    return
                frame = await asyncio.wait_for(relay.recv(), timeout=1.0) if stop is not None else await relay.recv()
                if isinstance(frame, bytes):
                    continue  # signaling carries JSON text; ignore anything else
                if is_control(frame):
                    if json.loads(frame).get("type") == "peer_joined":
                        log("bridge: the phone is here")
                    else:
                        log("bridge: the phone left")
                    continue
                if upstream is None:
                    ticket = await mint_gateway_ticket(gateway, key)
                    upstream = await websockets.connect(gateway_socket_url(gateway, ticket))
                    log("bridge: connected to the local gateway")
                    asyncio.create_task(_downstream(upstream, relay, log))
                await upstream.send(frame)
        finally:
            if upstream is not None:
                await upstream.close()


async def _downstream(upstream: Any, relay: Any, log) -> None:
    """Gateway → phone. Ends when either side closes."""
    try:
        async for frame in upstream:
            if isinstance(frame, str) and is_control(frame):
                continue
            await relay.send(frame)
    except Exception as error:  # the gateway closing ends this direction only
        log(f"bridge: upstream closed ({type(error).__name__})")
    finally:
        with contextlib.suppress(Exception):
            await relay.close()


def run_forever(relay: str, room: str, gateway: str, key: str, *, log=print) -> int:
    """Blocking entry point for the CLI."""
    stop = asyncio.Event()
    try:
        asyncio.run(bridge(relay, room, gateway, key, log=log, stop=stop))
    except KeyboardInterrupt:
        log("bridge: stopped")
    return 0


def describe(relay: str, room: str, gateway: str) -> str:
    """One status line for the operator, with no key material in it."""
    return f"relay {relay} · room {room[:8]}… · gateway {gateway}"
