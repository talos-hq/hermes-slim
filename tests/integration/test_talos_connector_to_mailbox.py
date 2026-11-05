"""End-to-end: the Talos connector posts to the real gateway mailbox endpoint.

Every other test covers one layer: the plugin against a stub, the router against a
TestClient. Nothing exercised the *seam* -- the connector's payload shape against the
router's schema, and its idempotency key against the store's deduplication. That is
where a silent drift would cost a notification with no error anywhere.

This file runs the real app on a loopback socket and drives it with the real
connector, so a schema mismatch fails here rather than on a phone.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from hermes_cli import web_server
from hermes_cli.web_routers import push as push_routes
from plugins.platforms.talos.notification import NotificationClient

TOKEN = "browser-token"
#: With a session token and auth disabled, the ticket principal resolves to this user.
USER = "legacy-token-owner"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def live_api(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", TOKEN)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)

    delivered: list[tuple[str, str]] = []

    async def fake_deliver(store, owner, message_id, settings, **kwargs):  # noqa: ANN001
        delivered.append((owner, message_id))

    monkeypatch.setattr(push_routes, "deliver", fake_deliver)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(web_server.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("gateway did not start on loopback")
    try:
        yield f"http://127.0.0.1:{port}", delivered
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _message(message_id: str = "msg-e2e-1", text: str = "The deploy finished.") -> dict[str, str]:
    """The connector accepts exactly this closed envelope and nothing else."""
    return {
        "id": message_id,
        "origin": "agent",
        "intent": "notify",
        "channel": "final",
        "kind": "text",
        "text": text,
    }


def test_connector_payload_is_accepted_by_the_mailbox_route(live_api):
    base, delivered = live_api
    client = NotificationClient(f"{base}/api/push/mailbox", TOKEN)

    result = asyncio.run(client.send_notification(USER, "session-1", _message()))
    assert result.success, result.error

    r = httpx.get(f"{base}/api/push/mailbox", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
    assert r.status_code == 200, r.text
    assert len(r.json()["messages"]) == 1
    # deliver() receives the owner key (a JSON identity), not the bare user id.
    assert [mid for _owner, mid in delivered] == ["msg-e2e-1"]
    assert USER in delivered[0][0]


def test_resending_the_same_message_does_not_duplicate_the_mailbox(live_api):
    base, delivered = live_api
    client = NotificationClient(f"{base}/api/push/mailbox", TOKEN)

    first = asyncio.run(client.send_notification(USER, "session-1", _message("msg-e2e-2")))
    second = asyncio.run(client.send_notification(USER, "session-1", _message("msg-e2e-2")))
    assert first.success and second.success

    r = httpx.get(f"{base}/api/push/mailbox", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
    assert len(r.json()["messages"]) == 1
    # One stored message means one wake-up, not two.
    assert [mid for _owner, mid in delivered] == ["msg-e2e-2"]


def test_tool_noise_never_reaches_the_mailbox(live_api):
    base, delivered = live_api
    client = NotificationClient(f"{base}/api/push/mailbox", TOKEN)

    # A well-formed envelope whose *text* carries tool output: the filter must catch it.
    noise = _message("msg-noise", "[tool] bash: rg -n foo returned 0 matches")
    result = asyncio.run(client.send_notification(USER, "session-1", noise))
    assert not result.success

    r = httpx.get(f"{base}/api/push/mailbox", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10)
    assert r.json()["messages"] == []
    assert delivered == []


def test_extra_envelope_fields_are_refused(live_api):
    base, delivered = live_api
    client = NotificationClient(f"{base}/api/push/mailbox", TOKEN)

    # Closed envelope: an extra key means someone is flattening structured blocks.
    widened = {**_message("msg-wide"), "tool_calls": "[]"}
    result = asyncio.run(client.send_notification(USER, "session-1", widened))
    assert not result.success
    assert delivered == []


def test_mailbox_requires_authentication(live_api):
    base, _ = live_api
    r = httpx.get(f"{base}/api/push/mailbox", timeout=10)
    assert r.status_code == 401


def test_recipient_must_match_the_authenticated_user(live_api):
    """The connector cannot post into someone else's mailbox."""
    base, delivered = live_api
    client = NotificationClient(f"{base}/api/push/mailbox", TOKEN)

    result = asyncio.run(client.send_notification("someone-else", "session-1", _message("msg-foreign")))
    assert not result.success
    assert "403" in (result.error or "")
    assert delivered == []
