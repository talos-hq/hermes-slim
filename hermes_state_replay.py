"""Verify the encrypted sidecar before replaying a protected transcript."""

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


def verify_protected_replay(db_path, session_id, binding, messages):
    """Authenticate the exact sidecar and the references in saved messages."""
    from hermes_cli.config import get_env_value

    try:
        from convo_safe.replay import open_bound_replay

        serialized_key = get_env_value(_MASTER_KEY_ENV) or ""
        if len(serialized_key) != 64:
            raise ValueError("invalid Keybox key length")
        master_key = bytes.fromhex(serialized_key)
        keybox = open_bound_replay(replay_log_path(db_path, session_id), master_key, binding)
        for value in _text_fields(messages):
            keybox.validate_references(keybox.replay_session, value)
    except (ImportError, OSError, ValueError, RuntimeError):
        raise ProtectedReplayError("Protected transcript replay could not be verified") from None
