"""Verify the encrypted sidecar before replaying a protected transcript."""

import json
from hashlib import sha256
from pathlib import Path

REPLAY_BINDING_KEY = "replay_binding"
_MASTER_KEY_ENV = "HERMES_CONVO_SAFE_MASTER_KEY"


class ProtectedReplayError(RuntimeError):
    """A protected transcript could not be authenticated for replay."""


def replay_log_path(db_path: Path, session_id: str) -> Path:
    """Derive a profile-local log path without trusting a stored filename."""
    filename = sha256(session_id.encode("utf-8")).hexdigest() + ".jsonl"
    return db_path.parent / "protected-transcripts" / filename


def require_plaintext_fallback_allowed(db_path: Path, session_id: str) -> None:
    """Block plaintext diversion when SQLite cannot supply a protected binding."""
    from hermes_cli.config import get_env_value

    if get_env_value(_MASTER_KEY_ENV):
        raise ProtectedReplayError("Protected transcript requires a verified fallback")
    log = replay_log_path(db_path, session_id)
    sidecar = log.with_name(log.name + ".keybox.sqlite")
    try:
        sidecar.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise ProtectedReplayError("Transcript protection could not be checked") from None
    raise ProtectedReplayError("Protected transcript requires a verified fallback")


def _text_fields(messages):
    pending = list(messages)
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)


def validate_replay_history(db, session_id, session_ids, messages):
    """Require a bound resume when the loaded lineage contains protected history."""
    missing = object()
    binding = db.get_session_model_config_value(session_id, REPLAY_BINDING_KEY, missing)
    if binding is not missing:
        verify_protected_replay(db.db_path, session_id, binding, messages)
    elif any(
        ancestor != session_id
        and db.get_session_model_config_value(ancestor, REPLAY_BINDING_KEY, missing) is not missing
        for ancestor in session_ids
    ):
        raise ProtectedReplayError("Protected transcript lineage has no replay binding")


def _open_sidecar(db_path, session_id, binding, *, writable=False):
    from hermes_cli.config import get_env_value

    try:
        from convo_safe.replay import open_bound_replay

        serialized_key = get_env_value(_MASTER_KEY_ENV) or ""
        if len(serialized_key) != 64:
            raise ValueError("invalid Keybox key length")
        master_key = bytes.fromhex(serialized_key)
        return open_bound_replay(replay_log_path(db_path, session_id), master_key, binding, writable=writable)
    except (ImportError, OSError, ValueError, RuntimeError):
        raise ProtectedReplayError("Protected transcript replay could not be verified") from None


def verify_protected_replay(db_path, session_id, binding, messages):
    """Authenticate the exact sidecar and the references in saved messages."""
    keybox = _open_sidecar(db_path, session_id, binding)
    try:
        for value in _text_fields(messages):
            keybox.validate_references(keybox.replay_session, value)
    except (ImportError, OSError, ValueError, RuntimeError):
        raise ProtectedReplayError("Protected transcript replay could not be verified") from None


def transcript_write_keybox(db_path, conn, session_id):
    """Resolve the binding in the same SQLite transaction as the protected write."""
    from hermes_state_sessions import _parse_model_config

    row = conn.execute("SELECT model_config FROM sessions WHERE id = ?", (session_id,)).fetchone()
    config = _parse_model_config(row[0]) if row is not None else {}
    if REPLAY_BINDING_KEY in config:
        return _open_sidecar(db_path, session_id, config[REPLAY_BINDING_KEY], writable=True)
    return None


def _project_value(keybox, value):
    if isinstance(value, str):
        return keybox.redact_model_text(keybox.replay_session, value, redact_email=True)
    if isinstance(value, list):
        return [_project_value(keybox, item) for item in value]
    if isinstance(value, dict):
        return {key: _project_value(keybox, item) for key, item in value.items()}
    return value


def _project_calls(keybox, calls):
    if not isinstance(calls, list):
        return _project_value(keybox, calls)
    projected = []
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if isinstance(function, dict) and isinstance(function.get("arguments"), str):
            serialized = function["arguments"]
            arguments = json.loads(serialized)
            safe = _project_value(keybox, arguments)
            call = {**call, "function": {**function, "arguments": serialized if safe == arguments else json.dumps(safe)}}
        projected.append(call)
    return projected


def project_transcript_row(keybox, message, tool_calls):
    """Project stored text without changing the occurrence or tool pairing IDs."""
    if keybox is None:
        return message, tool_calls
    try:
        projected = dict(message)
        for field in ("content", "api_content", "reasoning", "reasoning_content", "reasoning_details",
                      "codex_reasoning_items", "codex_message_items", "display_metadata"):
            if field in message:
                projected[field] = _project_value(keybox, message[field])
        return projected, _project_calls(keybox, tool_calls)
    except (ValueError, RuntimeError):
        raise ProtectedReplayError("Protected transcript could not be projected") from None


def write_user_message_content(db, session_id, row_id, content, display_metadata):
    """Rewrite the content of ONE known active user row. Used when a user turn was written at submit
    time (before the agent ran) and the turn prologue then rewrote the prompt it persists (@-file
    expansion, native image parts, a ``pre_llm_call`` veto's placeholder): the early row must show what
    the transcript will replay, not the raw keystrokes, and the turn must not append a second row for
    the same input. ``display_metadata``, when given, replaces the row's (and clears ``api_content``)."""
    if not session_id or isinstance(row_id, bool) or not isinstance(row_id, int) or row_id <= 0:
        return 0

    def _do(conn):
        box = transcript_write_keybox(db.db_path, conn, session_id)
        safe, _ = project_transcript_row(box, {"content": content, "display_metadata": display_metadata}, None)
        encoded = db._encode_content(safe["content"])
        where = "WHERE id = ? AND session_id = ? AND role = 'user' AND active = 1"
        if display_metadata is None:
            return conn.execute(f"UPDATE messages SET content = ? {where}", (encoded, row_id, session_id)).rowcount
        return conn.execute(f"UPDATE messages SET content = ?, api_content = NULL, display_metadata = ? {where}",
            (encoded, db._encode_display_metadata(safe["display_metadata"]), row_id, session_id)).rowcount

    return db._execute_write(_do)
