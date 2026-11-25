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


def test_request_middleware_sees_projected_input_and_its_output_is_projected(monkeypatch):
    from agent.turn_api_request import build_request_payload

    monkeypatch.setattr("agent.turn_api_request.request_keybox", lambda _agent: Projector())
    monkeypatch.setattr("agent.conversation_loop._redecorate_prompt_cache_for_provider",
                        lambda _agent, messages, **kwargs: (messages, None, kwargs["tools_for_api"]))
    monkeypatch.setattr("agent.turn_api_request.strip_images_for_rejecting_model", lambda *_: None)
    monkeypatch.setattr("agent.turn_api_request.observe_request_tools", lambda *_: None)
    monkeypatch.setattr("agent.turn_api_request.sanitize_outbound_kwargs", lambda *_: None)

    def middleware(payload, **_kwargs):
        assert payload["messages"][0]["content"] == "[email-marker]"
        return SimpleNamespace(
            payload={**payload, "instructions": "middleware fixture@example.org"},
            original_payload=payload,
            trace=[],
        )

    monkeypatch.setattr("hermes_cli.middleware.apply_llm_request_middleware", middleware)
    agent = SimpleNamespace(
        session_id="bound", model="provider/model", provider="openrouter", base_url="",
        api_mode="chat_completions", platform="cli", tools=[], _empty_content_retries=0,
        _reset_stream_delivery_tracking=lambda: None, _reapply_reasoning_echo_for_provider=lambda _: None,
        _build_api_kwargs=lambda messages: {"messages": messages},
    )
    result = build_request_payload(
        agent, api_messages=[{"role": "user", "content": "fixture@example.org"}],
        _moa_prepared_request=None, tools_for_api=[], system_message="", api_call_count=1,
        api_request_id="one", effective_task_id="test", turn_id="turn",
    )
    assert result.api_kwargs["instructions"] == "middleware [email-marker]"
    assert result.api_messages[0]["content"] == "[email-marker]"


def test_auxiliary_compression_projects_prompt_and_returned_summary(monkeypatch):
    from agent.context_compressor import ContextCompressor

    monkeypatch.setattr("agent.context_compressor.get_model_context_length", lambda *_: 100_000)
    monkeypatch.setattr("agent.context_compressor.session_request_keybox", lambda *_: Projector())
    observed = []

    def call_llm(**kwargs):
        assert kwargs["task"] == "compression"
        observed.extend(kwargs["messages"])
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="Recall fixture@example.org", reasoning_content=None),
        )])

    monkeypatch.setattr("agent.context_compressor.call_llm", call_llm)
    compressor = ContextCompressor(model="test", quiet_mode=True)
    summary = compressor._call_summary_llm("Summarize fixture@example.org", 0.0)
    assert observed == [{"role": "user", "content": "Summarize [email-marker]"}]
    assert summary == "Recall [email-marker]"
