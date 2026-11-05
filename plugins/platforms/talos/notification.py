"""Explicit notification boundary; never subscribe to transcript or logging streams."""

import asyncio
import hashlib
import json
import re
from urllib.parse import urlsplit

import httpx

from gateway.platforms.base import SendResult

_FIELDS = {"id", "origin", "intent", "channel", "kind", "text"}
_NOISE = re.compile(
    r"<\s*/?\s*(?:think|thinking|analysis|reasoning|tool\w*|function_call)\b"
    r"|\b(?:tool_call_id|tool_calls|tool_result|tool_output|function_call|"
    r"analysis_delta|reasoning_content)\b"
    r"|(?:^|\n)\s*(?:DEBUG|TRACE|INFO|WARNING|ERROR|Traceback)\b"
    r"|\[(?:DEBUG|TRACE|INFO|WARNING|ERROR|tool|analysis|reasoning)\]"
    r"|\b(?:analysis|reasoning|thinking)\s*:|<\|(?:analysis|tool|im_start)\|>"
    r"|\b\d{4}-\d{2}-\d{2}[T ][^\n]*\b(?:DEBUG|TRACE|INFO|WARNING|ERROR)\b"
    r"|(?:^|\n)\s*(?:assistant|tool|function)\s*(?:to=|:)"
    r"|\b(?:role|channel|type)\s*[\"']?\s*[:=]\s*[\"']?"
    r"(?:tool|function|debug|analysis|reasoning)\b",
    re.IGNORECASE,
)


def deliberate_message(message):
    """Accept one closed envelope, never flatten structured blocks or unknown metadata."""
    if type(message) is not dict or set(message) != _FIELDS:
        return False
    if any(type(value) is not str for value in message.values()):
        return False
    if (message["origin"], message["intent"], message["channel"], message["kind"]) != (
        "agent", "notify", "final", "text"
    ):
        return False
    text = message["text"]
    if not message["id"].strip() or not text.strip() or len(text) > 16_000:
        return False
    # Serialized envelopes and fenced blocks can hide tool output inside a text message.
    if _NOISE.search(text) or "```" in text or text.lstrip().startswith(("{", "[")):
        return False
    return True


class NotificationClient:
    """POST to a mailbox endpoint; the server must atomically deduplicate the key."""

    def __init__(self, endpoint, token, *, dry_run=False, attempts=3, backoff=0.25):
        self.endpoint = endpoint
        self.token = token
        self.dry_run = dry_run
        self.attempts = max(1, min(int(attempts), 5))
        self.backoff = max(0, min(float(backoff), 2))

    async def send_notification(self, user_id, session_id, message):
        if not deliberate_message(message):
            return SendResult(success=False, error="Not a deliberate agent notification")
        if not all(type(value) is str and value.strip() for value in (user_id, session_id)):
            return SendResult(success=False, error="User and session are required")
        # Scope the event identity to its recipient and session, surviving process restarts.
        identity = json.dumps([user_id, session_id, message["id"]], separators=(",", ":"))
        key = hashlib.sha256(identity.encode()).hexdigest()
        payload = {"user_id": user_id, "session_id": session_id,
                   "message": message["text"], "message_id": message["id"]}
        if self.dry_run:
            return SendResult(success=True, message_id=key, raw_response={"dry_run": True})
        if not self.endpoint or not self.token:
            return SendResult(success=False, error="Talos endpoint and token are required")
        url = urlsplit(self.endpoint)
        local_http = url.scheme == "http" and url.hostname in {"127.0.0.1", "::1"}
        if (url.scheme != "https" and not local_http) or not url.hostname or url.username or url.password or url.fragment:
            return SendResult(success=False, error="Talos requires HTTPS (HTTP only on loopback)")
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            for attempt in range(self.attempts):
                try:
                    response = await client.post(self.endpoint, json=payload, headers={
                        "Authorization": f"Bearer {self.token}", "Idempotency-Key": key,
                    })
                except httpx.TransportError:
                    status = None
                else:
                    status = response.status_code
                    if 200 <= status < 300:
                        return SendResult(success=True, message_id=key)
                    if status != 429 and not 500 <= status < 600:
                        return SendResult(success=False, error=f"Talos HTTP {status}")
                if attempt + 1 < self.attempts:
                    await asyncio.sleep(self.backoff * 2 ** attempt)
        # Own the retry budget; do not ask the base adapter to start another retry loop.
        return SendResult(success=False, error=f"Talos retry budget exhausted (HTTP {status})")
