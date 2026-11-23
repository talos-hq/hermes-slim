"""Correlate MCP session-sealed logs with their launching Hermes conversations."""

import json
import logging
import time
from uuid import uuid4

from tools.mcp_tool_common import mcp_field

logger = logging.getLogger("tools.mcp_tool")


def capture_launch(tool_name: str, args: dict, task_id: str) -> dict | None:
    """Snapshot the caller's route and config before crossing onto the MCP loop."""
    if tool_name != "session_start" or args.get("wait_seconds", 0) != 0:
        return None

    from gateway.session_context import async_delivery_supported, get_session_env
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_backend import ConfigBackendUnavailable

    parent = get_session_env("HERMES_SESSION_ID")
    if not parent or not async_delivery_supported():
        return None
    try:
        settings = load_config_readonly().get("mcp")
    except ConfigBackendUnavailable:
        logger.debug("MCP session-sealed settings unavailable", exc_info=True)
        return None
    if isinstance(settings, dict) and settings.get("session_sealed_notifications") is False:
        return None
    fields = ("platform", "chat_id", "chat_type", "thread_id", "user_id", "scope_id")
    return {
        **{name: get_session_env(f"HERMES_SESSION_{name.upper()}") for name in fields},
        "session_key": get_session_env("HERMES_SESSION_KEY"),
        "parent_session_id": parent,
        "origin_ui_session_id": get_session_env("HERMES_UI_SESSION_ID"),
        "owner_task_id": task_id,
        "started_at": time.time(),
    }


def _json_object(data) -> dict | None:
    if isinstance(data, str):
        try:
            value = json.loads(data)
        except (ValueError, RecursionError):
            return None
        return value if isinstance(value, dict) else None
    return None


def _identifier(value) -> bool:
    return isinstance(value, str) and value.isprintable() and bool(value.strip())


def _launch_handle(result) -> tuple[str, str | None] | None:
    if mcp_field(result, "is_error", "isError", False):
        return None
    structured = mcp_field(result, "structured_content", "structuredContent")
    blocks = getattr(result, "content", None) or ()
    for value in (structured, *(_json_object(getattr(block, "text", None)) for block in blocks)):
        if isinstance(value, dict) and _identifier(session_id := value.get("session_id")):
            turn_id = value.get("turn_id")
            return session_id, turn_id if _identifier(turn_id) else None
    return None


def _valid_payload(payload: dict) -> bool:
    exit_code = payload.get("exit_code")
    summary = payload.get("summary")
    elapsed = payload.get("elapsed_ms")
    schema = payload.get("schema")
    return (
        type(schema) is int and schema == 1
        and _identifier(payload.get("session_id")) and _identifier(payload.get("turn_id"))
        and payload.get("state") in ("idle", "failed")
        and "exit_code" in payload and (exit_code is None or type(exit_code) is int)
        and isinstance(summary, str) and len(summary) <= 400
        and (not summary or summary.splitlines() == [summary])
        and type(elapsed) is int and elapsed >= 0
    )


class SessionSealedNotifications:
    """One MCP transport's outstanding starts, accessed only on the MCP loop."""

    def __init__(self):
        self._pending: dict[str, tuple[str | None, dict]] = {}
        self._early: dict[tuple[str, str], dict] | None = None

    async def track_launch(self, owner: dict, call):
        # The RPC lock allows one launch at a time. A log can beat its tools/call
        # response, so hold early notices until the response proves ownership.
        self._early = {}
        try:
            result = await call
            if handle := _launch_handle(result):
                session_id, turn_id = handle
                self._pending[session_id] = (turn_id, owner)
                for early_handle, payload in self._early.items():
                    if early_handle[0] == session_id and (turn_id is None or turn_id == early_handle[1]):
                        self._deliver(payload)
                        break
                # A transport can stay up after a server abandons a watcher.
                if len(self._pending) > 1024:
                    self._pending.pop(next(iter(self._pending)))
            return result
        finally:
            self._early = None

    def handle_log(self, logger_name, data) -> None:
        if not isinstance(logger_name, str) or not logger_name.endswith(".session"):
            return
        if not _identifier(logger_name.removesuffix(".session")):
            return
        payload = _json_object(data)
        if payload is None:
            logger.debug("Ignoring malformed MCP session notification from %s", logger_name)
            return
        if payload.get("kind") != f"{logger_name}.sealed":
            return
        if not _valid_payload(payload):
            logger.debug("Ignoring malformed MCP session-sealed payload from %s", logger_name)
            return
        if not self._deliver(payload) and self._early is not None and len(self._early) < 16:
            self._early.setdefault((payload["session_id"], payload["turn_id"]), payload)

    def _deliver(self, payload: dict) -> bool:
        session_id = payload["session_id"]
        pending = self._pending.get(session_id)
        if pending is None:
            return False
        turn_id, owner = pending
        if turn_id is not None and turn_id != payload["turn_id"]:
            return True

        from agent.redact import redact_for_egress
        from tools.process_registry import process_registry

        del self._pending[session_id]
        process_registry.completion_queue.put({
            **owner,
            "type": "session_sealed",
            "event_id": uuid4().hex,
            "session_id": session_id,
            "turn_id": payload["turn_id"],
            "mcp_server": payload["kind"].removesuffix(".session.sealed"),
            "completion_reason": payload["state"],
            "exit_code": payload["exit_code"],
            "output": redact_for_egress(payload["summary"]),
            "elapsed_ms": payload["elapsed_ms"],
        })
        return True
