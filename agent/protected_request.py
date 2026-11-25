"""Project model request data for a session with a saved replay binding."""

import json

from hermes_state_replay import REPLAY_BINDING_KEY, ProtectedReplayError, _open_sidecar, _project_value, replay_log_path
from hermes_state_replay import require_replay_binding_if_recorded

# These fields belong to the transport. They do not carry transcript text.
_TRANSPORT_KEYS = {"model", "extra_headers", "timeout", "stream_options"}
_PROTOCOL_KEYS = {"id", "role", "type", "name", "tool_call_id", "call_id"}


def request_keybox(agent):
    """Open a writable Keybox only for a bound session."""
    return session_request_keybox(getattr(agent, "_session_db", None), getattr(agent, "session_id", None))


def session_request_keybox(store, session_id):
    """Resolve a saved binding for a request outside the main agent turn."""
    lookup = getattr(store, "get_session_model_config_value", None)
    if not session_id or not callable(lookup):
        return None
    absent = object()
    binding = lookup(session_id, REPLAY_BINDING_KEY, absent)
    path = getattr(store, "db_path", None)
    if binding is absent:
        require_replay_binding_if_recorded(path, session_id)
        return None
    if path is None:
        raise ProtectedReplayError("Protected provider request has no local transcript store")
    return _open_sidecar(path, session_id, binding, writable=True)


def _project(keybox, value, *, protocol=True):
    if isinstance(value, str):
        return keybox.redact_model_text(keybox.replay_session, value, redact_email=True)
    if isinstance(value, (tuple, list)):
        return [_project(keybox, item, protocol=protocol) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("type") in ("image", "image_url", "input_image", "input_audio", "input_file"):
        raise ProtectedReplayError("Protected provider request contains an unsupported attachment")
    if "encrypted_content" in value:
        raise ProtectedReplayError("Protected provider request contains opaque reasoning")
    projected = {}
    for name, item in value.items():
        if protocol and name in _PROTOCOL_KEYS:
            projected[name] = item
        elif protocol and name == "arguments" and isinstance(item, str):
            decoded = json.loads(item)
            safe = _project_value(keybox, decoded)
            projected[name] = item if safe == decoded else json.dumps(safe)
        else:
            projected[name] = _project(keybox, item, protocol=protocol)
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


def project_complete_response(keybox, assistant_message):
    """Project a complete provider message before it reaches response sinks."""
    if keybox is None:
        return
    try:
        assistant_message.content = _project(keybox, assistant_message.content)
        assistant_message.reasoning = _project(keybox, getattr(assistant_message, "reasoning", None))
        assistant_message.provider_data = _project(keybox, getattr(assistant_message, "provider_data", None))
        for call in assistant_message.tool_calls or ():
            arguments = call.function.arguments
            projected = _project(keybox, {"arguments": arguments})["arguments"]
            if projected != arguments or (isinstance(arguments, str) and "[[CONVO_SAFE:" in arguments):
                raise ProtectedReplayError("Protected tool arguments require local restoration")
    except (ValueError, RuntimeError):
        raise ProtectedReplayError("Protected provider response could not be projected") from None


def project_tool_result(agent, result):
    """Project the result before tool middleware or transcript sinks receive it."""
    keybox = request_keybox(agent)
    if keybox is None:
        return result
    if not isinstance(result, (str, dict, list, tuple, int, float, type(None))):
        raise ProtectedReplayError("Protected tool result uses an unsupported format")
    try:
        if isinstance(result, str) and result.lstrip().startswith(("{", "[")):
            try:
                decoded = json.loads(result)
            except ValueError:
                decoded = None
            if decoded is not None:
                safe = _project(keybox, decoded, protocol=False)
                return result if safe == decoded else json.dumps(safe, ensure_ascii=False)
        return _project(keybox, result, protocol=False)
    except (ValueError, RuntimeError):
        raise ProtectedReplayError("Protected tool result could not be projected") from None


def protected_tool_arguments(agent, arguments):
    """Return safe observer arguments and whether dispatch would require restoration."""
    keybox = request_keybox(agent)
    if keybox is None:
        return arguments, False
    try:
        projected = _project(keybox, arguments, protocol=False)
    except (ValueError, RuntimeError):
        return {}, True
    return projected, projected != arguments or "[[CONVO_SAFE:" in json.dumps(projected)


def project_compression_input(compressor, history):
    """Project structured records before the compressor serializes them."""
    keybox = session_request_keybox(compressor._session_db, compressor._session_id)
    return project_observer_history(keybox, history, None)[0]


def protected_child_config(store, parent_id, child_id, model_config):
    """Publish the child sidecar before SQLite makes the child visible."""
    keybox = session_request_keybox(store, parent_id)
    if keybox is None:
        return model_config
    from convo_safe.replay import replay_binding

    child_log = replay_log_path(store.db_path, child_id)
    try:
        keybox.fork_replay(child_log)
        # The cap is still the parent's binding. Opening only here obtains the
        # distinct ID generated by the child publication.
        from hermes_state_replay import open_unbound_sidecar

        child = open_unbound_sidecar(child_log)
        return {**(model_config or {}), REPLAY_BINDING_KEY: replay_binding(child)}
    except (OSError, ValueError, RuntimeError):
        raise ProtectedReplayError("Protected compression child could not be prepared") from None
