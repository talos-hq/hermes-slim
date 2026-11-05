"""Talos platform registration and manual notification command."""

import asyncio
import json

from agent.secret_scope import get_secret
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

from .notification import NotificationClient


class TalosAdapter(BasePlatformAdapter):
    def __init__(self, config):
        super().__init__(config, Platform("talos"))
        extra = config.extra or {}
        self.client = NotificationClient(
            extra.get("endpoint", ""), get_secret("TALOS_API_TOKEN", ""),
            dry_run=extra.get("dry_run", False) is True,
        )

    async def connect(self, *, is_reconnect=False):
        self._running = bool(self.client.dry_run or (self.client.endpoint and self.client.token))
        return self._running

    async def disconnect(self):
        self._running = False

    async def send_notification(self, user_id, session_id, message):
        return await self.client.send_notification(user_id, session_id, message)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        # No implicit promotion of ordinary replies, progress, tool previews or log text.
        if type(metadata) is not dict or set(metadata) != {"session_id", "notification"}:
            return SendResult(success=False, error="Explicit notification metadata required")
        message = metadata["notification"]
        if type(message) is not dict or content != message.get("text"):
            return SendResult(success=False, error="Notification content mismatch")
        return await self.send_notification(chat_id, metadata["session_id"], message)

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "dm"}


def setup_manual(parser):
    for name in ("endpoint", "user-id", "session-id", "message-id", "message"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--dry-run", action="store_true")


def manual_trigger(args):
    """Operator explicitly supplies the final notification, never a transcript file."""
    message = {"id": args.message_id, "origin": "agent", "intent": "notify",
               "channel": "final", "kind": "text", "text": args.message}
    client = NotificationClient(args.endpoint, get_secret("TALOS_API_TOKEN", ""),
                                dry_run=args.dry_run)
    result = asyncio.run(client.send_notification(args.user_id, args.session_id, message))
    print(json.dumps({"success": result.success, "message_id": result.message_id,
                      "error": result.error, "dry_run": args.dry_run}))
    return 0 if result.success else 1


def register(ctx):
    ctx.register_platform(name="talos", label="Talos", adapter_factory=TalosAdapter,
                          check_fn=lambda: True, allow_update_command=False)
    ctx.register_cli_command("talos", "Send an explicit Talos notification",
                             setup_manual, manual_trigger)
