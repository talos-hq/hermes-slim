#!/usr/bin/env python3
"""Paired wall-clock delegation capture benchmark; scripted API, real lifecycle and brain CLI.

Run with the Hermes test interpreter. No credentials or writes to the operator's brain.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def benchmark(pairs: int) -> dict:
    from run_agent import AIAgent
    from tools.brain_tool import brain_enabled
    from agent.shared_brain import retrieve

    if not brain_enabled():
        raise RuntimeError("Benchmark requires the enabled brain CLI on PATH")
    from tools.delegate_tool_results import _run_child_lifecycle

    def create(**kwargs):
        return NS(choices=[NS(message=NS(
            content="Solution: Added widget include directory; verified with `make widget`.",
            tool_calls=None, reasoning=None, reasoning_content=None,
        ), finish_reason="stop")], usage=None, model="test-model")

    def delegate(capture):
        child = AIAgent(model="test-model", api_key="test", base_url="http://localhost:1/v1",
            platform="cli", enabled_toolsets=[], max_iterations=2, quiet_mode=True,
            skip_memory=True, skip_context_files=True, skip_background_review=True)
        child._disable_streaming = True
        started = time.perf_counter()
        if capture:
            entry = _run_child_lifecycle(0, "Fix widget compilation", child, None)
        else:
            with patch("tools.delegate_tool_brain.capture_delegation_outcome", lambda *args: None):
                entry = _run_child_lifecycle(0, "Fix widget compilation", child, None)
        elapsed = (time.perf_counter() - started) * 1000
        if entry["status"] != "completed":
            raise RuntimeError(f"Benchmark delegation failed: {entry.get('error')}")
        return elapsed

    off, on, deltas = [], [], []
    with patch("agent.process_bootstrap.OpenAI", lambda **kw: NS(chat=NS(completions=NS(create=create)))):
        # Warm imports, config, the CLI, and DB initialization in both arms.
        delegate(False)
        delegate(True)
        for index in range(pairs):
            samples = {enabled: delegate(enabled) for enabled in ([False, True] if index % 2 == 0 else [True, False])}
            off.append(samples[False])
            on.append(samples[True])
            deltas.append(samples[True] - samples[False])
    if "Added widget include directory" not in retrieve("Widget compilation", str(ROOT)):
        raise RuntimeError("Capture did not produce a retrievable brain entry")
    baseline = statistics.median(off)
    noise = statistics.median(abs(value - baseline) for value in off)
    overhead = statistics.median(deltas)
    return {"pairs": pairs, "scripted_api": True, "real_delegation_lifecycle": True,
            "capture_off_median_ms": round(baseline, 3),
            "capture_on_median_ms": round(statistics.median(on), 3),
            "paired_overhead_median_ms": round(overhead, 3),
            "baseline_mad_ms": round(noise, 3), "below_measured_noise": abs(overhead) < noise,
            "off_ms": [round(v, 3) for v in off], "on_ms": [round(v, 3) for v in on]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=20)
    args = parser.parse_args()
    if args.pairs < 2:
        parser.error("--pairs must be at least 2")
    with tempfile.TemporaryDirectory(prefix="brain-delegation-", dir=ROOT) as directory:
        with patch.dict(os.environ, {"HERMES_HOME": str(Path(directory) / "home"),
                                     "HERMES_BRAIN_DIR": str(Path(directory) / "brain")}):
            result = benchmark(args.pairs)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
