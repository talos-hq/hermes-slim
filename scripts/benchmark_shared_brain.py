#!/usr/bin/env python3
"""Offline fixed-task benchmark through AIAgent; no claim of model-quality improvement.

Run with the Hermes test interpreter: python scripts/benchmark_shared_brain.py
Requires the separately installed brain CLI. All writes go to a temporary directory.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
TASK = "Widget compilation fails due to missing include directory. What verified fix is known?"


def worker() -> dict:
    from types import SimpleNamespace as NS
    from unittest.mock import patch
    from run_agent import AIAgent
    from agent.shared_brain import retrieve

    prompts = []

    def create(**kwargs):
        prompts.extend(str(m["content"]) for m in kwargs["messages"] if m["role"] == "system")
        return NS(choices=[NS(message=NS(content="Offline scripted response", tool_calls=None,
            reasoning=None, reasoning_content=None), finish_reason="stop")], usage=None, model="test-model")

    with patch("agent.process_bootstrap.OpenAI", lambda **kw: NS(chat=NS(completions=NS(create=create)))):
        agent = AIAgent(model="test-model", api_key="test", base_url="http://localhost:1/v1",
            platform="cli", enabled_toolsets=[], max_iterations=2, quiet_mode=True,
            skip_memory=True, skip_context_files=True, skip_background_review=True)
        agent._disable_streaming = True
        started = time.perf_counter()
        result = agent.run_conversation(TASK)
        turn_ms = (time.perf_counter() - started) * 1000
    timings = []
    matches = 0
    for _ in range(20):
        started = time.perf_counter()
        matches += bool(retrieve(TASK, str(ROOT)))
        timings.append((time.perf_counter() - started) * 1000)
    tool_calls = sum(len(m.get("tool_calls") or []) for m in result["messages"] if m["role"] == "assistant")
    return {"prompt_chars": sum(map(len, prompts)), "model_tool_calls": tool_calls,
            "recall_in_prompt": any("<shared_brain>" in prompt for prompt in prompts),
            "model_tokens": None, "turn_ms": round(turn_ms, 2),
            "retrieval_matches_out_of_20": matches,
            "retrieval_median_ms": round(statistics.median(timings), 2),
            "retrieval_max_ms": round(max(timings), 2)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--work-dir", type=Path, default=ROOT)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    if args.worker:
        print("BRAIN_BENCH_RESULT=" + json.dumps(worker()))
        return
    from hermes_platform.resolver.core import locate_command

    command = locate_command("brain").command
    if not command:
        parser.error("install the existing brain CLI first")
    with tempfile.TemporaryDirectory(prefix="brain-bench-", dir=args.work_dir) as directory:
        root = Path(directory)
        env = {**os.environ, "HERMES_BRAIN_DIR": str(root / "brain"), "TERMINAL_CWD": str(ROOT)}
        subprocess.run([*command, "add", "--kind", "solution", "--scope", "repo:widget", "--",
                        "Widget compilation fixed by adding include directory; verified build passed"],
                       env=env, check=True, capture_output=True, timeout=5)
        results = {}
        for mode in ("off", "on"):
            home = root / mode
            home.mkdir()
            (home / "config.yaml").write_text(
                json.dumps({"shared_brain": {"enabled": mode == "on"}}), encoding="utf-8")
            proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker"],
                                  cwd=ROOT, env={**env, "HERMES_HOME": str(home)},
                                  capture_output=True, text=True, timeout=60, check=False)
            if proc.returncode:
                raise RuntimeError(proc.stdout + proc.stderr)
            marker = next(line for line in proc.stdout.splitlines() if line.startswith("BRAIN_BENCH_RESULT="))
            results[mode] = json.loads(marker.split("=", 1)[1])
        print(json.dumps({"task": TASK, "results": results,
                          "limitations": "Scripted model, zero network calls; no live token usage or task-success comparison. "
                          "Retrieval timing is 20 real brain searches in each worker, even the off worker."}, indent=2))


if __name__ == "__main__":
    main()
