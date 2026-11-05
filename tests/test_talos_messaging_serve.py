"""The agent's end of the Talos Messaging relay bridge.

A phone on cellular and a machine behind a router cannot dial each other, so both
dial the public relay and this bridge forwards JSON-RPC between the relay's room
and the local gateway's ``/api/ws``. These tests stand up a real relay and a real
gateway socket on loopback and drive a full round trip through the bridge.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
import websockets

from hermes_cli import talos_messaging_serve as bridge

KEY = "K" * 43
ROOM = hashlib.sha256(KEY.encode()).hexdigest()[:32]


def test_the_room_is_the_key_digest_prefix():
    assert bridge.room_for(KEY) == ROOM
    assert len(ROOM) == 32
    assert KEY not in ROOM


def test_the_relay_url_names_the_room_and_the_role():
    url = bridge.relay_socket_url("wss://clud.bot/talos-messaging/signal", ROOM, "agent")
    assert url == f"wss://clud.bot/talos-messaging/signal/{ROOM}?role=agent"


def test_control_frames_are_recognised_and_nothing_else_is():
    assert bridge.is_control('{"type":"peer_joined"}')
    assert bridge.is_control('{"type":"peer_left"}')
    assert not bridge.is_control('{"jsonrpc":"2.0","method":"prompt.submit"}')
    assert not bridge.is_control("not json")


def test_the_gateway_socket_url_switches_scheme():
    assert bridge.gateway_socket_url("http://127.0.0.1:9120", "t1") == "ws://127.0.0.1:9120/api/ws?ticket=t1"
    assert bridge.gateway_socket_url("https://example.test/", "t2") == "wss://example.test/api/ws?ticket=t2"


@pytest.mark.asyncio
async def test_frames_round_trip_through_a_real_relay_and_gateway():
    """phone → relay → bridge → gateway → bridge → relay → phone."""
    seen_gateway: list[str] = []
    seen_phone: list[str] = []
    joined = asyncio.Event()
    delivered = asyncio.Event()

    async def gateway_handler(ws):
        async for frame in ws:
            seen_gateway.append(frame)
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"text": "hello from the agent"}}))

    # The gateway's ticket route is HTTP; this harness is a socket-only stand-in,
    # and the ticket contract is covered by the routing tests.
    async def fake_ticket(_gateway, _key, **_kwargs):
        return "test-ticket"

    original_ticket = bridge.mint_gateway_ticket
    bridge.mint_gateway_ticket = fake_ticket  # type: ignore[assignment]

    seats: dict[str, object] = {}

    async def relay_handler(ws):
        """A two-seat room, exactly as clud.bot implements it: one `agent`, one `phone`."""
        role = ws.request.path.split("role=")[-1]
        seats[role] = ws
        # Tell the side that was already waiting that the other arrived.
        for name, other in seats.items():
            if other is not ws:
                await other.send('{"type":"peer_joined"}')
        try:
            async for frame in ws:
                for name, other in seats.items():
                    if other is not ws:
                        await other.send(frame)
        except websockets.ConnectionClosed:
            pass
        finally:
            seats.pop(role, None)

    gateway_server = await websockets.serve(gateway_handler, "127.0.0.1", 0)
    relay_server = await websockets.serve(relay_handler, "127.0.0.1", 0)
    gateway_port = gateway_server.sockets[0].getsockname()[1]
    relay_port = relay_server.sockets[0].getsockname()[1]
    stop = asyncio.Event()
    logs: list[str] = []

    async def phone():
        await asyncio.sleep(0.15)
        url = f"ws://127.0.0.1:{relay_port}/talos-messaging/signal/{ROOM}?role=phone"
        async with websockets.connect(url) as ws:
            await ws.recv()  # peer_joined
            joined.set()
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "prompt.submit", "params": {"text": "hi"}}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            seen_phone.append(reply)
            delivered.set()
            stop.set()

    # Both ends dial out: the phone and the bridge run concurrently, exactly as
    # they do through a real relay.
    phone_task = asyncio.create_task(phone())
    bridge_task = asyncio.create_task(
        bridge.bridge(
            f"ws://127.0.0.1:{relay_port}/talos-messaging/signal",
            ROOM,
            f"http://127.0.0.1:{gateway_port}",
            KEY,
            log=logs.append,
            stop=stop,
        )
    )
    try:
        await asyncio.wait_for(joined.wait(), timeout=5)
        await asyncio.wait_for(delivered.wait(), timeout=8)
        await asyncio.wait_for(phone_task, timeout=5)
    finally:
        bridge.mint_gateway_ticket = original_ticket  # type: ignore[assignment]
        phone_task.cancel()
        bridge_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bridge_task
        gateway_server.close()
        relay_server.close()
        await gateway_server.wait_closed()
        await relay_server.wait_closed()

    # The gateway saw exactly the phone's frame — no relay housekeeping.
    assert [json.loads(frame)["method"] for frame in seen_gateway] == ["prompt.submit"]
    # And the phone saw the gateway's reply.
    assert seen_phone[0]["result"]["text"] == "hello from the agent"
    # The bridge never logs a payload.
    assert not any("hello from the agent" in line for line in logs)


@pytest.mark.asyncio
async def test_the_relay_address_comes_from_the_published_document():
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"websocket": "wss://clud.bot/talos-messaging/signal/{room}?role=agent|phone"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, _url):
            return Response()

    import hermes_cli.talos_messaging_serve as module

    original = module.httpx.AsyncClient
    module.httpx.AsyncClient = lambda **_kwargs: Client()  # type: ignore[assignment]
    try:
        assert await bridge.resolve_relay(None) == "wss://clud.bot/talos-messaging/signal"
        assert await bridge.resolve_relay("ws://127.0.0.1:9000/signal") == "ws://127.0.0.1:9000/signal"
    finally:
        module.httpx.AsyncClient = original
