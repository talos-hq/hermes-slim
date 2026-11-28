"""Provider safety-filter refusals classify as content_policy_blocked, never as a retryable outage."""

import itertools

import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from agent.turn_api_request import TOOL_HISTORY_CONTEXT_HEADER, flatten_replayed_tool_history
from agent.turn_recovery import recover_after_classification
from agent.turn_retry_state import TurnRetryState


class _APIError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = {}


@pytest.mark.parametrize("status, message", [
    # Output guardrail relayed by OpenRouter / the Nous Portal as an in-band 502 marked provider_unavailable.
    (502, "Upstream error from Alibaba: Output data may contain inappropriate content."),
    (None, "Upstream error from Alibaba: Output data may contain inappropriate content."),
    # Input guardrail: DashScope's documented 400 (code data_inspection_failed).
    (400, "Error code: 400 - {'error': {'message': 'Input data may contain inappropriate content.', "
          "'type': 'data_inspection_failed', 'code': 'data_inspection_failed'}}"),
])
def test_alibaba_guardrail_is_a_content_policy_block_not_an_outage(status, message):
    """Alibaba Model Studio's guardrail refuses this content; retrying the same request repeats the
    refusal, so it must not ride the 5xx / unknown retry ladder."""
    result = classify_api_error(_APIError(message, status), provider="nous", model="qwen/qwen3.8-flash")
    assert result.reason == FailoverReason.content_policy_blocked
    assert result.retryable is False


def test_openrouter_sensitive_info_guardrail_stops_without_fallback():
    error = _APIError(
        "Request blocked by content filter: Content filter redaction would produce invalid tool call arguments",
        403,
    )
    result = classify_api_error(error, provider="openrouter")
    assert result.reason == FailoverReason.content_policy_blocked
    assert result.retryable is False
    assert result.should_fallback is False


def test_openrouter_guardrail_retries_without_replaying_tool_argument_values():
    error = _APIError(
        "Request blocked by content filter: Content filter redaction would produce invalid tool call arguments",
        403,
    )
    refusal = classify_api_error(error, provider="openrouter")
    retry = TurnRetryState()
    assistant = {
        "role": "assistant",
        "tool_calls": [
            {"id": "probe", "type": "function",
             "function": {"name": "check", "arguments": '{"identifier":2025550100}'}},
        ],
    }
    tool_result = {"role": "tool", "tool_call_id": "probe", "content": "Check complete."}
    final = {"role": "assistant", "content": "The check completed."}
    user = {"role": "user", "content": "Continue."}
    history = [assistant, tool_result, final, user]
    retry_now, rotated = recover_after_classification(
        None, error, refusal, retry, status_code=403, error_context={},
        messages=history, api_messages=history,
    )

    assert retry_now and not rotated
    assert retry.openrouter_tool_history_flattened
    replay = flatten_replayed_tool_history(history)
    # Tool output goes in a user-role context message, never in assistant content (#181).
    # The call carried no prose, so no assistant message is kept for it.
    assert [m["role"] for m in replay] == ["user", "assistant", "user"]
    assert all("tool_calls" not in m for m in replay)
    assert all("Historical" not in str(m["content"]) for m in replay)
    assert replay[0]["content"].startswith(TOOL_HISTORY_CONTEXT_HEADER)
    assert "check returned:\n> Check complete." in replay[0]["content"]
    assert replay[1]["content"] == "The check completed."
    assert replay[2] == user
    assert history[0]["tool_calls"][0]["function"]["arguments"] == '{"identifier":2025550100}'
    assert flatten_replayed_tool_history(replay) is replay


def test_openrouter_guardrail_projection_survives_another_tool_round(monkeypatch):
    from types import SimpleNamespace

    from openai.types.chat import ChatCompletion

    from run_agent import AIAgent

    calls = []
    captured = '{"identifier":2025550100}'
    guardrail_error = _APIError(
        "Request blocked by content filter: Content filter redaction would produce invalid tool call arguments",
        403,
    )

    def create(**kwargs):
        arguments = {
            tc["id"]: tc["function"]["arguments"]
            for message in kwargs["messages"]
            for tc in message.get("tool_calls", [])
        }
        calls.append((arguments, kwargs["messages"]))
        if arguments:
            raise guardrail_error
        message = {"role": "assistant", "content": "Done"}
        finish = "stop"
        if len(calls) == 2:
            message = {
                "role": "assistant", "content": None, "tool_calls": [
                    {"id": "next", "type": "function",
                     "function": {"name": "unavailable_probe", "arguments": '{"identifier":17}'}},
                ],
            }
            finish = "tool_calls"
        return ChatCompletion(
            id="guardrail-probe", object="chat.completion", model="test-model", created=1,
            choices=[{"index": 0, "message": message, "finish_reason": finish}],
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr("agent.process_bootstrap.OpenAI", lambda **kwargs: client)
    monkeypatch.setattr("model_tools.get_tool_definitions", lambda *a, **k: [])
    agent = AIAgent(
        model="test-model", api_key="test-key", provider="openrouter",
        base_url="https://openrouter.ai/api/v1", max_iterations=4,
        quiet_mode=True, skip_memory=True, skip_context_files=True,
    )
    agent._disable_streaming = True
    history = [
        {"role": "user", "content": "Check the identifier."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "historical", "type": "function",
             "function": {"name": "check", "arguments": captured}},
        ]},
        {"role": "tool", "tool_call_id": "historical", "content": "Check complete."},
    ]

    result = agent.run_conversation("Continue.", conversation_history=history)

    assert result["completed"]
    assert [arguments for arguments, _ in calls] == [{"historical": captured}, {}, {}]
    assert all(
        any(row.get("role") == "user" and "check returned:\n> Check complete." in (row.get("content") or "")
            for row in request)
        and not any(row.get("role") == "assistant" and "returned:" in (row.get("content") or "")
                    for row in request)
        for _, request in calls[1:]
    )
    assert history[1]["tool_calls"][0]["function"]["arguments"] == captured


def test_flattened_history_never_puts_tool_output_in_assistant_turns():
    """Adversarial shapes: empty call text, parallel calls, a call at the end, multimodal user."""
    image_user = {"role": "user", "content": [
        {"type": "text", "text": "Look."}, {"type": "image_url", "image_url": {"url": "data:x"}}]}
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Go."},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "terminal", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "line1\nline2"},
        {"role": "tool", "tool_call_id": "b", "content": ""},
        image_user,
        {"role": "assistant", "content": "Checking.", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "terminal", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c", "content": "ok"},
    ]
    replay = flatten_replayed_tool_history(history)
    roles = [m["role"] for m in replay]
    assert roles == ["system", "user", "assistant", "user"]
    assert all(a != b for a, b in itertools.pairwise(roles[1:]))
    for message in replay:
        assert "tool_calls" not in message
        if message["role"] == "assistant":
            assert "returned:" not in message["content"] and "Historical" not in message["content"]
    merged = replay[1]["content"]
    assert isinstance(merged, list)
    texts = [part.get("text", "") for part in merged]
    assert texts[0].startswith("Go.\n\n" + TOOL_HISTORY_CONTEXT_HEADER)
    assert "terminal returned:\n> line1\n> line2" in texts[0]
    assert "read_file returned:\n> (no textual result)" in texts[0]
    assert texts[1] == "Look."
    assert {"type": "image_url", "image_url": {"url": "data:x"}} in merged
    assert replay[2]["content"] == "Checking."
    assert "terminal returned:\n> ok" in replay[3]["content"]
    assert flatten_replayed_tool_history(replay) is replay
