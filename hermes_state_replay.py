"""Verify the encrypted sidecar before replaying a protected transcript."""

import json
import re
from hashlib import sha256
from pathlib import Path

REPLAY_BINDING_KEY = "replay_binding"
_MASTER_KEY_ENV = "HERMES_CONVO_SAFE_MASTER_KEY"
_LITERAL_KEYS_ENV = "HERMES_CONVO_SAFE_LITERAL_KEYS"
_REPLAY_MARKERS = re.compile(r"(\[\[CONVO_SAFE:[^\n]*?\]\])")


class ProtectedReplayError(RuntimeError):
    """A protected transcript could not be authenticated for replay."""


def replay_log_path(db_path: Path, session_id: str) -> Path:
    """Derive a profile-local log path without trusting a stored filename."""
    filename = sha256(session_id.encode("utf-8")).hexdigest() + ".jsonl"
    return db_path.parent / "protected-transcripts" / filename


def require_plaintext_fallback_allowed(db_path: Path, session_id: str) -> None:
    """Block plaintext diversion when SQLite cannot supply a protected binding."""
    from hermes_cli.config import get_env_value

    if get_env_value(_MASTER_KEY_ENV) or _has_replay_files(db_path, session_id):
        raise ProtectedReplayError("Protected transcript requires a verified fallback")


def _has_replay_files(db_path: Path, session_id: str) -> bool:
    log = replay_log_path(db_path, session_id)
    for path in (log.with_name(log.name + ".keybox.sqlite"), log.with_suffix(".binding.json")):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ProtectedReplayError("Transcript protection could not be checked") from None
        return True
    return False


def require_replay_binding_if_recorded(db_path, session_id):
    """Reject an apparently unbound session when there is durable protection evidence."""
    if db_path is not None and session_id and _has_replay_files(db_path, session_id):
        raise ProtectedReplayError("Protected transcript is missing its saved replay binding")


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
        and (db.get_session_model_config_value(ancestor, REPLAY_BINDING_KEY, missing) is not missing
             or _has_replay_files(db.db_path, ancestor))
        for ancestor in session_ids
    ):
        raise ProtectedReplayError("Protected transcript lineage has no replay binding")
    else:
        require_replay_binding_if_recorded(db.db_path, session_id)


def _master_key():
    from hermes_cli.config import get_env_value

    serialized_key = get_env_value(_MASTER_KEY_ENV) or ""
    if len(serialized_key) != 64:
        raise ValueError("invalid Keybox key length")
    return bytes.fromhex(serialized_key)


def open_unbound_sidecar(path):
    """Read identity from a freshly created sidecar during publication."""
    from convo_safe import Keybox

    return Keybox.open_replay(path, _master_key())


def fork_replay_config(db_path, keybox, child_id, model_config):
    """Prepare a distinct authenticated child binding before its row is published."""
    if keybox is None:
        return model_config
    from convo_safe.replay import replay_binding

    child_log = replay_log_path(db_path, child_id)
    try:
        keybox.fork_replay(child_log)
        child = open_unbound_sidecar(child_log)
        return {**(model_config or {}), REPLAY_BINDING_KEY: replay_binding(child)}
    except (OSError, ValueError, RuntimeError):
        raise ProtectedReplayError("Protected replay child could not be prepared") from None


def _open_sidecar(db_path, session_id, binding, *, writable=False):
    try:
        from convo_safe.replay import open_bound_replay

        return open_bound_replay(replay_log_path(db_path, session_id), _master_key(), binding, writable=writable)
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
        from hermes_state_replay_fallback import remember_fallback_binding

        binding = config[REPLAY_BINDING_KEY]
        keybox = _open_sidecar(db_path, session_id, binding, writable=True)
        remember_fallback_binding(db_path, session_id, binding)
        return keybox
    require_replay_binding_if_recorded(db_path, session_id)
    return None


def project_session_prompt(db, conn, session_id, prompt, *, model_config=None):
    """Use the current binding, or the binding being published with a new child."""
    if prompt is None:
        return None
    if model_config and REPLAY_BINDING_KEY in model_config:
        keybox = _open_sidecar(db.db_path, session_id, model_config[REPLAY_BINDING_KEY], writable=True)
    else:
        keybox = transcript_write_keybox(db.db_path, conn, session_id)
    return prompt if keybox is None else _project_value(keybox, prompt)


def project_replay_text(keybox, text):
    """Apply automatic detection and exact matches for opted-in profile secrets."""
    from hermes_cli.config import get_env_value

    configured = get_env_value(_LITERAL_KEYS_ENV)
    if not configured:
        return keybox.redact_model_text(keybox.replay_session, text, redact_email=True)
    values = set()
    for name in configured.split(","):
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ProtectedReplayError("Protected literal redaction key list is invalid")
        value = get_env_value(name)
        if not value or "[[CONVO_SAFE:" in value:
            raise ProtectedReplayError("A protected literal redaction value is unavailable or invalid")
        values.add(value)
    literals = re.compile("|".join(re.escape(value) for value in sorted(values, key=len, reverse=True)))

    def redact_match(match):
        return keybox.get_or_create(keybox.replay_session, "CREDENTIAL", match[0])

    parts = _REPLAY_MARKERS.split(text)
    for index in range(0, len(parts), 2):
        parts[index] = literals.sub(redact_match, parts[index])
    return keybox.redact_model_text(keybox.replay_session, "".join(parts), redact_email=True)


def _project_value(keybox, value):
    if isinstance(value, str):
        return project_replay_text(keybox, value)
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
