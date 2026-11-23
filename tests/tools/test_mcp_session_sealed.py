"""Sealed session notices stay with the conversation that launched the turn."""

import asyncio
import json
import sys
import textwrap
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, LoggingMessageNotificationParams, TextContent

from gateway.session_context import scoped_current_session_id, scoped_session_key
from hermes_cli.config import atomic_config_write
from hermes_constants import get_hermes_home
from tools import mcp_tool, mcp_tool_handlers
from tools.process_registry import process_registry
from tools.process_registry_notifications import format_process_notification


def _payload(**updates):
    return dict(kind="clud.session.sealed", schema=1, session_id="clud-1",
                turn_id="turn-1", state="idle", exit_code=0,
                summary="Report ready", elapsed_ms=1200) | updates


def _notice(data, logger="clud.session"):
    return LoggingMessageNotificationParams(logger=logger, level="info", data=data)


@pytest.fixture
def connection(monkeypatch):
    queue = Queue()
    monkeypatch.setattr(process_registry, "completion_queue", queue)
    server = mcp_tool.MCPServerTask("configured-alias")
    server.session = SimpleNamespace(call_tool=AsyncMock())
    monkeypatch.setattr(mcp_tool_handlers, "_trust_gate_check", lambda *_: None)
    monkeypatch.setattr(mcp_tool_handlers, "_acquire_call_server", lambda *_: (server, None))
    monkeypatch.setattr(mcp_tool_handlers._loop, "_run_on_mcp_loop",
                        lambda call, timeout: asyncio.run(call()))
    return server, server._make_logging_callback(), queue


@pytest.mark.parametrize("early", [False, True])
def test_mcp_session_sealed_enqueues_once_for_launching_session(connection, early):
    server, callback, queue = connection
    notice = _notice(json.dumps(_payload()))

    async def launch(*_args, **_kwargs):
        if early:
            await callback(notice)
        return CallToolResult(content=[TextContent(
            type="text", text=json.dumps({"session_id": "clud-1", "turn_id": "turn-1"}))])

    server.session.call_tool.side_effect = launch
    with scoped_current_session_id("hermes-1"), scoped_session_key("chat-1"):
        mcp_tool_handlers._make_tool_handler(server.name, "session_start", 2)({"wait_seconds": 0})

    with scoped_current_session_id("hermes-2"), scoped_session_key("chat-2"):
        if not early:
            asyncio.run(callback(notice))
        asyncio.run(callback(notice))  # duplicate notice

    event = queue.get_nowait()
    assert queue.empty()
    assert event["type"] == "session_sealed"
    assert event["event_id"]
    assert event["parent_session_id"] == "hermes-1"
    assert event["session_key"] == "chat-1"
    assert event["session_id"] == "clud-1"
    assert event["turn_id"] == "turn-1"
    assert event["exit_code"] == 0
    assert event["output"] == "Report ready"
    assert format_process_notification(event) == (
        "[IMPORTANT: clud session clud-1 sealed (state: idle, exit code 0).\n"
        "Summary: Report ready\nInspect this session with the clud session tools.]"
    )


@pytest.mark.parametrize("data, logger", [
    ("{broken json", "clud.session"),
    ("[]", "clud.session"),
    (json.dumps(_payload(kind="clud.session.started")), "clud.session"),
    (json.dumps(_payload(schema=True)), "clud.session"),
    (json.dumps(_payload(state=[])), "clud.session"),
    (json.dumps(_payload(summary="line one\nline two")), "clud.session"),
    (json.dumps(_payload(turn_id="different-turn")), "clud.session"),
    (json.dumps(_payload()), "other"),
])
def test_mcp_session_sealed_ignores_invalid_notifications(connection, data, logger):
    server, callback, queue = connection
    server.session.call_tool.return_value = CallToolResult(
        content=[], structured_content={"session_id": "clud-1", "turn_id": "turn-1"})
    with scoped_current_session_id("hermes-1"), scoped_session_key("chat-1"):
        mcp_tool_handlers._make_tool_handler(server.name, "session_start", 2)({"wait_seconds": 0})
    asyncio.run(callback(_notice(data, logger)))
    assert queue.empty()


def test_mcp_session_sealed_toggle_off(connection):
    atomic_config_write(get_hermes_home() / "config.yaml",
                        {"mcp": {"session_sealed_notifications": False}})
    server, callback, queue = connection
    server.session.call_tool.return_value = CallToolResult(
        content=[], structured_content={"session_id": "clud-1", "turn_id": "turn-1"})
    with scoped_current_session_id("hermes-1"), scoped_session_key("chat-1"):
        mcp_tool_handlers._make_tool_handler(server.name, "session_start", 2)({"wait_seconds": 0})
    asyncio.run(callback(_notice(json.dumps(_payload()))))
    assert queue.empty()


@pytest.mark.no_isolate
@pytest.mark.platforms("posix")
def test_mcp_session_sealed_over_stdio(monkeypatch, tmp_path):
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.mcp_tool_loop import _stop_mcp_loop
    from tools.registry import registry

    script = tmp_path / "server.py"
    script.write_text(textwrap.dedent(f"""\
        from mcp.server.mcpserver import Context, MCPServer
        app = MCPServer("clud")
        launch_session = None

        @app.tool()
        async def session_start(ctx: Context, wait_seconds: int = 0) -> dict:
            global launch_session
            launch_session = ctx.session
            return {{"session_id": "clud-1", "turn_id": "turn-1"}}

        @app.tool()
        async def release_seal() -> str:
            await launch_session.send_log_message("info", {json.dumps(_payload())!r}, "clud.session")
            return "sent"

        app.run()
        """), encoding="utf-8")
    atomic_config_write(get_hermes_home() / "config.yaml",
                        {"mcp_servers": {"clud": {"command": sys.executable, "args": [str(script)]}}})
    queue = Queue()
    monkeypatch.setattr(process_registry, "completion_queue", queue)

    try:
        assert "mcp__clud__session_start" in discover_mcp_tools()
        with scoped_current_session_id("launch-owner"), scoped_session_key("launch-route"):
            result = registry.dispatch("mcp__clud__session_start", {"wait_seconds": 0})
            assert "clud-1" in result
        with scoped_current_session_id("other-owner"), scoped_session_key("other-route"):
            result = registry.dispatch("mcp__clud__release_seal", {})
            assert "sent" in result
        event = queue.get(timeout=5)
        assert event["type"] == "session_sealed"
        assert event["parent_session_id"] == "launch-owner"
        assert event["session_key"] == "launch-route"
        assert event["output"] == "Report ready"
        assert queue.empty()
    finally:
        shutdown_mcp_servers()
        _stop_mcp_loop()
