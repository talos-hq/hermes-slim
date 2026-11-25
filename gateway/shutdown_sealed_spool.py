"""Encrypt queued messages whose transcript is unavailable at shutdown."""

import base64
import hashlib
import hmac
import json
import os


def _context(payload):
    return json.dumps([payload.get(key) for key in ("session_key", "ts", "seq", "reason")]).encode()


def _cipher():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from hermes_state_replay import _master_key

    key = hmac.digest(_master_key(), b"hermes-shutdown-spool-v1", hashlib.sha256)
    return AESGCM(key)


def seal_pending(data, payload):
    """Bind an encrypted record to the original route and queue position."""
    nonce = os.urandom(12)
    encrypted = _cipher().encrypt(nonce, json.dumps(data, ensure_ascii=False).encode(), _context(payload))
    return {"sealed_v1": base64.b64encode(nonce + encrypted).decode("ascii")}


def unseal_pending(data, payload):
    """Unseal only in the owning profile when the route can be resolved."""
    encrypted = base64.b64decode(data["sealed_v1"], validate=True)
    return json.loads(_cipher().decrypt(encrypted[:12], encrypted[12:], _context(payload)))
