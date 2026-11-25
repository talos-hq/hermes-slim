"""The protected outgoing request retains protocol fields and projects model text."""

from types import SimpleNamespace

import pytest

from agent.protected_request import project_observer_history, project_request, request_keybox
from hermes_state_replay import ProtectedReplayError


class Projector:
    replay_session = 4

    def redact_model_text(self, scope, value, *, redact_email):
        assert scope == 4 and redact_email
        return value.replace("fixture@example.org", "[email-marker]")


def test_project_request_covers_instructions_content_and_tool_arguments():
    keybox = Projector()
    request = {
        "model": "provider/model",
        "extra_headers": {"Authorization": "Bearer transport-token"},
        "instructions": "Reply to fixture@example.org.",
        "messages": [{"role": "user", "content": "fixture@example.org"}],
        "input": [{"type": "function_call", "call_id": "call-1", "name": "mail",
                   "arguments": r'{"recipient":"fixture\u0040example.org"}'}],
    }
    safe = project_request(keybox, request)
    assert safe["instructions"] == "Reply to [email-marker]."
    assert safe["messages"] == [{"role": "user", "content": "[email-marker]"}]
    assert safe["input"][0]["call_id"] == "call-1"
    assert "[email-marker]" in safe["input"][0]["arguments"]
    assert safe["extra_headers"] == request["extra_headers"]
    assert project_request(keybox, safe) == safe
    assert request["messages"][0]["content"] == "fixture@example.org"


def test_request_fails_closed_for_bound_worker_without_a_local_store():
    store = SimpleNamespace(get_session_model_config_value=lambda *_: {})
    with pytest.raises(ProtectedReplayError, match="no local transcript store"):
        request_keybox(SimpleNamespace(session_id="bound", _session_db=store))


def test_opaque_inputs_and_raw_observer_history():
    keybox = Projector()
    with pytest.raises(ProtectedReplayError):
        project_request(keybox, {"input": [{"type": "input_image", "image_url": "https://example.org"}]})
    history, user = project_observer_history(keybox, [{"content": "fixture@example.org"}], "fixture@example.org")
    assert history == [{"content": "[email-marker]"}]
    assert user == "[email-marker]"
