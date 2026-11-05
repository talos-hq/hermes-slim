"""Mailbox behavior over real HTTP, with no external services or credentials."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from plugins.platforms.talos.adapter import TalosAdapter, manual_trigger, setup_manual
from plugins.platforms.talos.notification import NotificationClient


@pytest.fixture
def mailbox():
    state = SimpleNamespace(requests=[], messages={}, statuses=[])

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            key = self.headers["Idempotency-Key"]
            state.requests.append((self.path, dict(self.headers), body))
            status = state.statuses.pop(0) if state.statuses else 201
            if 200 <= status < 300:
                state.messages.setdefault(key, body)
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.endpoint = f"http://127.0.0.1:{server.server_port}/notifications"
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def message():
    return {"id": "event-42", "origin": "agent", "intent": "notify",
            "channel": "final", "kind": "text", "text": "Your report is ready."}


def send(mailbox, message, **options):
    client = NotificationClient(mailbox.endpoint, "test-token", **options)
    return asyncio.run(client.send_notification("user-1", "session-2", message))


def test_payload_and_duplicate(mailbox, message):
    first = send(mailbox, message)
    second = send(mailbox, message)
    assert first.success and second.success
    assert first.message_id == second.message_id
    assert len(mailbox.requests) == 2
    assert len(mailbox.messages) == 1
    path, headers, body = mailbox.requests[0]
    assert path == "/notifications"
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["Idempotency-Key"] == first.message_id
    assert body == {"user_id": "user-1", "session_id": "session-2",
                    "message_id": "event-42", "message": "Your report is ready."}


def test_retry_backoff_then_success(mailbox, message, monkeypatch):
    waits = []

    async def record_wait(delay):
        waits.append(delay)

    monkeypatch.setattr("plugins.platforms.talos.notification.asyncio.sleep", record_wait)
    mailbox.statuses = [503, 500, 201]
    assert send(mailbox, message).success
    assert waits == [0.25, 0.5]
    assert len({headers["Idempotency-Key"] for _, headers, _ in mailbox.requests}) == 1
    assert len(mailbox.messages) == 1


@pytest.mark.parametrize("status", [401, 403, 400, 302])
def test_permanent_failure_not_retried(mailbox, message, status):
    mailbox.statuses = [status, 201]
    assert not send(mailbox, message).success
    assert len(mailbox.requests) == 1
    assert not mailbox.messages


def test_retry_budget(mailbox, message):
    mailbox.statuses = [503] * 6
    assert not send(mailbox, message, attempts=2, backoff=0).success
    assert len(mailbox.requests) == 2


def test_dry_run_no_requests(mailbox, message):
    result = send(mailbox, message, dry_run=True)
    assert result.success and result.raw_response == {"dry_run": True}
    assert not mailbox.requests and not mailbox.messages


@pytest.mark.parametrize("noise", [
    {"role": "tool", "content": "secret"},
    {"type": "tool_result", "content": "secret"},
    [{"type": "text", "text": "hello"}, {"type": "thinking", "thinking": "secret"}],
    '{"role":"tool","content":"secret"}',
    'Summary: {"role":"tool","content":"secret"}',
    '<think>private reasoning</think>Ready',
    '<tool_response>secret</tool_response>',
    'INFO tool completed\nsecret',
    '```json\n{"tool_calls": []}\n```',
    'assistant to=terminal: secret',
    'reasoning_content: secret',
    'Update: [DEBUG] private data',
    '2026-11-05T12:00:00 DEBUG private data',
    'Summary: reasoning: private thoughts',
    '<|analysis|>private thoughts',
])
def test_wrapped_noise_dropped(mailbox, message, noise):
    message["text"] = noise
    assert not send(mailbox, message).success
    assert not mailbox.requests


@pytest.mark.parametrize("change", [
    {"origin": "tool"}, {"intent": "reply"}, {"channel": "analysis"},
    {"kind": "debug"}, {"tool_output": "secret"}, {"metadata": {"role": "tool"}},
])
def test_provenance_rejected(mailbox, message, change):
    message.update(change)
    assert not send(mailbox, message).success
    assert not mailbox.requests


def test_adapter_boundary_and_manual_trigger(mailbox, message, monkeypatch, capsys):
    import argparse

    monkeypatch.setattr("plugins.platforms.talos.adapter.get_secret", lambda *args: "test-token")
    adapter = TalosAdapter(SimpleNamespace(extra={"endpoint": mailbox.endpoint}))
    assert asyncio.run(adapter.connect())
    assert not asyncio.run(adapter.send("user-1", "ordinary reply")).success
    bad = {"session_id": "session-2", "notification": message, "tool_output": "secret"}
    assert not asyncio.run(adapter.send("user-1", message["text"], metadata=bad)).success
    assert not mailbox.requests
    metadata = {"session_id": "session-2", "notification": message}
    assert asyncio.run(adapter.send("user-1", message["text"], metadata=metadata)).success
    parser = argparse.ArgumentParser()
    setup_manual(parser)
    args = parser.parse_args([
        "--endpoint", mailbox.endpoint, "--user-id", "user-1", "--session-id", "session-2",
        "--message-id", "manual-1", "--message", "Manual notification", "--dry-run",
    ])
    assert manual_trigger(args) == 0
    assert json.loads(capsys.readouterr().out)["dry_run"]
    assert len(mailbox.requests) == 1
    args.dry_run = False
    assert manual_trigger(args) == 0
    assert len(mailbox.requests) == 2
    assert any(body["message"] == "Manual notification" for body in mailbox.messages.values())
    asyncio.run(adapter.disconnect())


def test_real_plugin_discovery_and_manual_command(mailbox, monkeypatch, capsys):
    import argparse

    from gateway.platform_registry import platform_registry
    from hermes_cli.main import _resolve_deferred_platform_cli_command
    from hermes_cli.plugins import discover_plugins, get_plugin_manager

    discover_plugins()
    _resolve_deferred_platform_cli_command("talos")
    entry = platform_registry.get("talos")
    assert entry is not None
    assert entry.check_fn()
    adapter = entry.adapter_factory(SimpleNamespace(extra={"dry_run": True}))
    assert asyncio.run(adapter.connect())
    command = get_plugin_manager()._cli_commands["talos"]
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)
    args = parser.parse_args([
        "--endpoint", mailbox.endpoint, "--user-id", "user", "--session-id", "session",
        "--message-id", "event", "--message", "Discovery works", "--dry-run",
    ])
    assert command["handler_fn"](args) == 0
    assert json.loads(capsys.readouterr().out)["success"]
    assert not mailbox.requests


def test_dry_run_never_constructs_http_client(mailbox, message, monkeypatch):
    def forbidden_client(**kwargs):
        raise AssertionError("Dry-run must not create a network client")

    monkeypatch.setattr("plugins.platforms.talos.notification.httpx.AsyncClient", forbidden_client)
    assert send(mailbox, message, dry_run=True).success
    assert not mailbox.requests
