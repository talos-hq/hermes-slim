#!/usr/bin/env python3
"""Ask BrightKB a question and print its answer. Standard library only.

Usage:
    python3 brightkb.py "What are the benefits of turmeric?"
    python3 brightkb.py --markdown "question" ["second question" ...]

The endpoint comes from BRIGHTKB_URL and defaults to the hosted instance. Only
the last user message is read; nothing is remembered between requests.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "https://brightkb.onrender.com/v1"
TIMEOUT_SECONDS = 60


def base_url() -> str:
    return os.environ.get("BRIGHTKB_URL", DEFAULT_URL).rstrip("/")


def ask(question: str, markdown: bool = False, url: str | None = None) -> object:
    """POST one question to BrightKB; return parsed JSON, or text in markdown mode."""
    body = json.dumps(
        {
            "model": "ika-knowledge-base",
            "messages": [{"role": "user", "content": question}],
            # Ask for the format explicitly: the operator can change the default in Settings.
            "response_format": {"type": "text" if markdown else "json_object"},
        }
    ).encode()
    request = urllib.request.Request(
        (url or base_url()) + "/chat/completions",
        body,
        {"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        content = json.load(response)["choices"][0]["message"]["content"]
    if markdown:
        return content
    try:
        return json.loads(content)
    except ValueError:
        return {"question": question, "text": content}


def ask_many(questions: list[str], markdown: bool = False, url: str | None = None) -> list:
    return [ask(question, markdown=markdown, url=url) for question in questions]


def _fail(message: str) -> None:
    sys.exit(message)


def main(argv: list[str]) -> int:
    markdown = "--markdown" in argv
    questions = [argument for argument in argv if argument != "--markdown"]
    if not questions:
        print(__doc__)
        return 2
    answers = []
    for question in questions:
        try:
            answers.append(ask(question, markdown=markdown))
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:300]
            if error.code == 403 and "terms_not_accepted" in detail:
                _fail(
                    "BrightKB has not accepted its own terms of use on this server yet: "
                    f"{base_url()} answers 403 until the operator sets "
                    "BRIGHTKB_ACCEPT_TERMS=1 (or chooses 'I agree' in its web UI)."
                )
            _fail(f"BrightKB error {error.code}: {detail}")
        except urllib.error.URLError as error:
            _fail(f"BrightKB is not reachable at {base_url()} ({error.reason}).")
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8")
    if markdown:
        print("\n\n".join(
            f"# {question}\n\n{answer}" for question, answer in zip(questions, answers)
        ) if len(answers) > 1 else answers[0])
    else:
        print(json.dumps(answers[0] if len(answers) == 1 else answers,
                         indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
