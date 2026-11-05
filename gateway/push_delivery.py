"""Encrypted Web Push wake-ups; mailbox history is always the source of truth."""

import asyncio
import base64
import json
from dataclasses import dataclass, field

import requests
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush


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
        except WebPushException as exc:
            return exc.response.status_code if exc.response is not None else None
        except requests.RequestException:
            return None
        return response.status_code


async def deliver(store, owner, message_id, settings, *, backoff=0.25):
    """One bounded batch per admitted event; do not log provider errors or secrets."""
    if not settings.private_key or not settings.subject:
        return
    payload = json.dumps({'message_id': message_id})
    for device_id, subscription in store.subscriptions(owner):
        for attempt in range(3):
            status = await asyncio.to_thread(_send_once, subscription, payload, settings)
            if status in (404, 410):
                store.revoke(owner, device_id, subscription)
                break
            if status is not None and status != 429 and not 500 <= status < 600:
                break
            if attempt < 2:
                await asyncio.sleep(backoff * 2 ** attempt)
