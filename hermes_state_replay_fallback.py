"""Store and verify the Keybox binding independently of the SQLite transcript."""

import hmac
import json

from hermes_state_replay import (
    ProtectedReplayError,
    _master_key,
    _open_sidecar,
    _project_value,
    project_transcript_row,
    replay_log_path,
    require_plaintext_fallback_allowed,
)
from utils import atomic_json_write


def _binding_path(log):
    return log.with_suffix(".binding.json")


def _binding_tag(master, session_id, binding):
    payload = json.dumps([session_id, binding], sort_keys=True, separators=(",", ":")).encode()
    return hmac.digest(master, b"Hermes transcript fallback v1\x00" + payload, "sha256").hex()


def _read_binding(log, session_id):
    contents = _binding_path(log).read_text(encoding="utf-8")
    envelope = json.loads(contents)
    binding = envelope["binding"]
    expected = _binding_tag(_master_key(), session_id, binding)
    if not hmac.compare_digest(expected, envelope["tag"]):
        raise ValueError("invalid fallback binding")
    return binding


def remember_fallback_binding(db_path, session_id, binding):
    """Commit the fallback binding before the first protected transcript write."""
    log = replay_log_path(db_path, session_id)
    try:
        try:
            existing = _read_binding(log, session_id)
        except FileNotFoundError:
            tag = _binding_tag(_master_key(), session_id, binding)
            atomic_json_write(_binding_path(log), {"binding": binding, "tag": tag}, mode=0o600, fsync_dir=True)
        else:
            if existing != binding:
                raise ValueError("fallback binding already belongs to a different sidecar")
    except (KeyError, OSError, TypeError, ValueError):
        raise ProtectedReplayError("Protected fallback binding could not be saved") from None


def project_fallback_records(db_path, session_id, records):
    """Return the projected records and their protected log path, when bound."""
    log = replay_log_path(db_path, session_id)
    try:
        binding = _read_binding(log, session_id)
    except FileNotFoundError:
        require_plaintext_fallback_allowed(db_path, session_id)
        return records, None
    except (KeyError, OSError, TypeError, ValueError):
        raise ProtectedReplayError("Protected fallback binding could not be verified") from None
    keybox = _open_sidecar(db_path, session_id, binding, writable=True)
    projected = []
    for record in records:
        if record is None:
            continue
        row = record if isinstance(record, dict) else {"content": str(record)}
        try:
            calls = row.get("tool_calls")
            if isinstance(calls, str):
                calls = json.loads(calls)
            safe, calls = project_transcript_row(keybox, row, calls)
            for field in set(row) - {"role", "tool_calls", "tool_call_id", "tool_name", "message_uid"}:
                if safe[field] is row[field]:
                    safe[field] = _project_value(keybox, row[field])
            if "tool_calls" in row:
                safe["tool_calls"] = calls
            json.dumps(safe)
        except (TypeError, ValueError, RuntimeError):
            raise ProtectedReplayError("Protected fallback transcript could not be projected") from None
        projected.append(safe)
    return projected, log
