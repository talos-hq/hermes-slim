"""Tests for the brightkb bundled skill.

Covers ``scripts/brightkb.py`` without touching the network: the request shape
it sends, how it unwraps the OpenAI-style envelope, markdown vs JSON mode, the
``BRIGHTKB_URL`` override, and the terms-not-accepted path that a freshly
deployed server answers until its operator accepts BrightKB's terms.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[2] / "skills" / "research" / "brightkb"
SCRIPT = SKILL_DIR / "scripts" / "brightkb.py"


@pytest.fixture(scope="module")
def brightkb():
    spec = importlib.util.spec_from_file_location("brightkb_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload: dict) -> None:
        self._raw = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc) -> None:
        return None


class _Recorder:
    """Stands in for urllib.request.urlopen and records the request."""

    def __init__(self, content: str, outer: dict | None = None) -> None:
        self.content = content
        self.outer = outer
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request, timeout=None):
        self.requests.append((request.full_url, json.loads(request.data)))
        payload = self.outer or {
            "choices": [{"message": {"content": self.content}}],
        }
        return _Response(payload)


def test_default_endpoint_is_the_hosted_server(brightkb) -> None:
    assert brightkb.DEFAULT_URL == "https://brightkb.onrender.com/v1"
    assert brightkb.base_url().endswith("/v1")


def test_env_override_strips_trailing_slash(brightkb, monkeypatch) -> None:
    monkeypatch.setenv("BRIGHTKB_URL", "http://127.0.0.1:27495/v1/")
    assert brightkb.base_url() == "http://127.0.0.1:27495/v1"


def test_ask_posts_one_user_message_and_json_format(brightkb, monkeypatch) -> None:
    recorder = _Recorder('{"facts": [{"text": "turmeric is anti-inflammatory"}]}')
    monkeypatch.setattr(brightkb.urllib.request, "urlopen", recorder)

    answer = brightkb.ask("What are the benefits of turmeric?")

    assert answer == {"facts": [{"text": "turmeric is anti-inflammatory"}]}
    url, body = recorder.requests[0]
    assert url == "https://brightkb.onrender.com/v1/chat/completions"
    assert body["model"] == "ika-knowledge-base"
    assert body["messages"] == [
        {"role": "user", "content": "What are the benefits of turmeric?"}
    ]
    assert body["response_format"] == {"type": "json_object"}


def test_markdown_mode_returns_text_and_asks_for_text(brightkb, monkeypatch) -> None:
    recorder = _Recorder("# Turmeric\n\nSome sources.")
    monkeypatch.setattr(brightkb.urllib.request, "urlopen", recorder)

    answer = brightkb.ask("Tell me about turmeric", markdown=True)

    assert answer == "# Turmeric\n\nSome sources."
    assert recorder.requests[0][1]["response_format"] == {"type": "text"}


def test_non_json_content_is_wrapped_not_crashed(brightkb, monkeypatch) -> None:
    recorder = _Recorder("plain text answer")
    monkeypatch.setattr(brightkb.urllib.request, "urlopen", recorder)

    answer = brightkb.ask("question")

    assert answer == {"question": "question", "text": "plain text answer"}


def test_terms_not_accepted_403_explains_the_operator_switch(brightkb, monkeypatch, capsys) -> None:
    body = json.dumps(
        {"error": {"type": "terms_not_accepted", "message": "not agreed"}}
    ).encode()
    error = urllib.error.HTTPError(
        "https://brightkb.onrender.com/v1/chat/completions", 403, "Forbidden", {}, BytesIO(body)
    )

    def raise_error(request, timeout=None):
        raise error

    monkeypatch.setattr(brightkb.urllib.request, "urlopen", raise_error)

    with pytest.raises(SystemExit) as exit_info:
        brightkb.main(["What is turmeric?"])

    message = str(exit_info.value)
    assert "BRIGHTKB_ACCEPT_TERMS" in message
    assert "terms" in message.lower()


def test_ask_many_preserves_question_order(brightkb, monkeypatch) -> None:
    answers = iter(["first", "second"])
    recorder = _Recorder("unused")

    def respond(request, timeout=None):
        recorder.requests.append((request.full_url, json.loads(request.data)))
        return _Response({"choices": [{"message": {"content": json.dumps(
            {"answer": next(answers)})}}]})

    monkeypatch.setattr(brightkb.urllib.request, "urlopen", respond)

    result = brightkb.ask_many(["q1", "q2"])

    assert [item["answer"] for item in result] == ["first", "second"]
    assert [body["messages"][0]["content"] for _, body in recorder.requests] == ["q1", "q2"]


def test_skill_frontmatter_names_the_hosted_default() -> None:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "brightkb.onrender.com" in text
    assert "BRIGHTKB_URL" in text
