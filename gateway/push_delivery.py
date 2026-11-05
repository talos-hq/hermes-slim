"""Encrypted Web Push wake-ups; mailbox history is always the source of truth."""

import asyncio
import base64
import json
from datetime import UTC, datetime
from dataclasses import dataclass, field
from typing import Any

import requests
from cryptography.hazmat.primitives import serialization

# The Web Push stack ships as an optional extra. Import it defensively so a base install can still
# start the gateway and serve the mailbox: an unguarded import here raised ModuleNotFoundError at
# module load and took the whole push router (and any app importing it) down with it.
try:  # pragma: no cover - the missing-extra path is covered by a dedicated test
    from py_vapid import Vapid as _VapidImpl
    from pywebpush import WebPushException as _WebPushError
    from pywebpush import webpush as _webpushImpl

    PUSH_SDK_AVAILABLE = True
    PUSH_SDK_MISSING = ""
    _PUSH_ERRORS: tuple[type[BaseException], ...] = (_WebPushError,)
except ModuleNotFoundError as exc:  # pragma: no cover
    _VapidImpl = None
    _webpushImpl = None
    PUSH_SDK_AVAILABLE = False
    PUSH_SDK_MISSING = exc.name or "pywebpush"
    _PUSH_ERRORS = ()

# Typed loosely on purpose: the real classes are absent in a base install, and the call sites are
# guarded by PUSH_SDK_AVAILABLE before they run.
Vapid: Any = _VapidImpl
webpush: Any = _webpushImpl


class PushSdkMissing(RuntimeError):
    """Raised when signing is attempted without the optional Web Push extra installed."""


@dataclass(frozen=True)
class PushSettings:
    private_key: str = field(repr=False)
    subject: str

    @classmethod
    def load(cls):
        from gateway.platforms._shared import get_scoped_secret
        from hermes_cli.config_effective import load_user_config_effective
        block = load_user_config_effective().get('push', {})
        return cls(get_scoped_secret('TALOS_VAPID_PRIVATE_KEY', ''), block.get('subject', ''))

    def vapid(self):
        if not PUSH_SDK_AVAILABLE:
            raise PushSdkMissing(PUSH_SDK_MISSING)
        # Only encoded key material, never a browser-supplied filename.
        return Vapid.from_string(self.private_key)

    def public_key(self):
        key = self.vapid().public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(key).rstrip(b'=').decode()


class _PushSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs['allow_redirects'] = False
        return super().request(method, url, **kwargs)


def _send_once(subscription, payload, settings):
    with _PushSession() as client:
        client.trust_env = False
        try:
            response = webpush(subscription, payload, vapid_private_key=settings.vapid(),
                               vapid_claims={'sub': settings.subject}, timeout=10, ttl=300,
                               requests_session=client)
        except _PUSH_ERRORS as exc:
            response_obj = getattr(exc, "response", None)
            return response_obj.status_code if response_obj is not None else None
        except requests.RequestException:
            return None
        return response.status_code


async def deliver(store, owner, message_id, settings, *, backoff=0.25):
    """One bounded batch per admitted event; do not log provider errors or secrets."""
    if not PUSH_SDK_AVAILABLE:
        # Mailbox remains authoritative; a base install simply cannot wake a device.
        return
    if not settings.private_key or not settings.subject:
        return
    payload = json.dumps({'message_id': message_id})
    for device_id, subscription in store.subscriptions(owner):
        for attempt in range(3):
            attempted_at = datetime.now(UTC).isoformat()
            try:
                status = await asyncio.to_thread(_send_once, subscription, payload, settings)
            except (ValueError, TypeError):
                store.record_delivery(owner, attempted_at, 'failed')
                raise
            store.record_delivery(owner, attempted_at,
                                  'sent' if status is not None and 200 <= status < 300 else 'failed')
            if status in (404, 410):
                store.revoke(owner, device_id, subscription, reason=str(status))
                break
            if status is not None and status != 429 and not 500 <= status < 600:
                break
            if attempt < 2:
                await asyncio.sleep(backoff * 2 ** attempt)
