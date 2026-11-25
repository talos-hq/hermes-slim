"""Project model request data for a session with a saved replay binding."""

import json

from hermes_state_replay import REPLAY_BINDING_KEY, ProtectedReplayError, _open_sidecar, _project_value

# These fields belong to the transport. They do not carry transcript text.
_TRANSPORT_KEYS = {"model", "extra_headers", "timeout", "stream_options"}
_PROTOCOL_KEYS = {"id", "role", "type", "name", "tool_call_id", "call_id"}


def request_keybox(agent):
    """Open a writable Keybox only for a bound session."""
    store = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    lookup = getattr(store, "get_session_model_config_value", None)
    if not session_id or not callable(lookup):
        return None
    absent = object()
    binding = lookup(session_id, REPLAY_BINDING_KEY, absent)
    if binding is absent:
        return None
    path = getattr(store, "db_path", None)
    if path is None:
        raise ProtectedReplayError("Protected provider request has no local transcript store")
    return _open_sidecar(path, session_id, binding, writable=True)


def _project(keybox, value):
    if isinstance(value, str):
        return keybox.redact_model_text(keybox.replay_session, value, redact_email=True)
    if isinstance(value, (tuple, list)):
        return [_project(keybox, item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("type") in ("image", "image_url", "input_image", "input_audio", "input_file"):
        raise ProtectedReplayError("Protected provider request contains an unsupported attachment")
    if "encrypted_content" in value:
        raise ProtectedReplayError("Protected provider request contains opaque reasoning")
    projected = {}
    for name, item in value.items():
        if name in _PROTOCOL_KEYS:
            projected[name] = item
        elif name == "arguments" and isinstance(item, str):
            decoded = json.loads(item)
            safe = _project_value(keybox, decoded)
            projected[name] = item if safe == decoded else json.dumps(safe)
        else:
            projected[name] = _project(keybox, item)
    return projected


def project_request(keybox, request):
    """Return a projected copy, preserving transport configuration."""
    if keybox is None:
        return request
    try:
        return {name: value if name in _TRANSPORT_KEYS else _project(keybox, value)
                for name, value in request.items()}
    except (ValueError, RuntimeError):
        raise ProtectedReplayError("Protected provider request could not be projected") from None


def project_observer_history(keybox, messages, user_text):
    """Keep the request observer's history and user payload at the same boundary."""
    if keybox is None:
        return messages, user_text
    try:
        return _project(keybox, messages), _project(keybox, user_text)
    except (ValueError, RuntimeError):
        raise ProtectedReplayError("Protected request history could not be projected") from None
