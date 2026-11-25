"""Encrypt queued messages whose transcript is unavailable at shutdown."""

import base64
import hashlib
import hmac
import json
import os
import uuid
from pathlib import Path


def _profile_identity(*, create=False):
    from agent.secret_scope import current_secret_scope_home
    from hermes_constants import get_hermes_home
    from utils import fsync_directory, mkstemp_beside

    home = Path(current_secret_scope_home() or get_hermes_home())
    identity = home / ".shutdown-spool-owner"
    if create and not identity.exists():
        fd, name = mkstemp_beside(identity)
        candidate = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(os.urandom(32))
                stream.flush()
                os.fsync(stream.fileno())
            # Publish without replacing an identity that another writer already installed.
            try:
                os.link(candidate, identity)
            except FileExistsError:
                pass
            fsync_directory(home)
        finally:
            candidate.unlink(missing_ok=True)
    value = identity.read_bytes()
    if len(value) != 32:
        raise ValueError("Invalid shutdown spool profile identity")
    return value


def _context(payload, *, create=False):
    metadata = json.dumps([payload.get(key) for key in ("session_key", "ts", "seq", "reason")]).encode()
    return _profile_identity(create=create) + metadata


def _key(label):
    from hermes_state_replay import _master_key

    return hmac.digest(_master_key(), label, hashlib.sha256)


def _cipher():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(_key(b"hermes-shutdown-spool-v1"))


def seal_pending(data, payload):
    """Bind an encrypted record to the original route and queue position."""
    nonce = os.urandom(12)
    encrypted = _cipher().encrypt(nonce, json.dumps(data, ensure_ascii=False).encode(), _context(payload, create=True))
    return {"sealed_v1": base64.b64encode(nonce + encrypted).decode("ascii")}


def unseal_pending(data, payload):
    """Unseal only in the owning profile when the route can be resolved."""
    encrypted = base64.b64decode(data["sealed_v1"], validate=True)
    return json.loads(_cipher().decrypt(encrypted[:12], encrypted[12:], _context(payload)))


def sealed_pending_receipt(data):
    return hashlib.sha256(base64.b64decode(data["sealed_v1"], validate=True)).hexdigest()


def _bound_tag(data, payload, *, create=False):
    content = json.dumps([payload["replay_session"], payload["replay_id"], data], sort_keys=True).encode()
    return hmac.digest(_key(b"hermes-shutdown-bound-v1"), _context(payload, create=create) + content, "sha256").hex()


def bind_pending(data, payload, session_id):
    """Authenticate both the redacted content and its original destination."""
    bound = {**payload, "replay_session": session_id, "replay_id": uuid.uuid4().hex}
    return {**bound, "replay_tag": _bound_tag(data, bound, create=True), "data": data}


def verify_bound_pending(data, payload):
    if not hmac.compare_digest(payload["replay_tag"], _bound_tag(data, payload)):
        raise ValueError("Invalid protected shutdown record")
    return payload["replay_tag"]


def append_protected_pending(db, session_id, receipt_id, payload, text):
    """Commit the replay receipt and its redacted message atomically."""
    receipt = "gateway.sealed_spool.v1." + receipt_id
    messages = [{"role": "user", "content": text, "timestamp": payload.get("ts")}]

    def write(conn):
        if conn.execute("SELECT 1 FROM state_meta WHERE key=?", (receipt,)).fetchone():
            return
        db._append_messages_in_transaction(conn, session_id, messages)
        conn.execute("INSERT INTO state_meta(key,value) VALUES (?,?)", (receipt, session_id))

    db._execute_transcript_write(write, messages, patience_s=db._TRANSCRIPT_WRITE_PATIENCE_S)
