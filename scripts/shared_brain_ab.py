#!/usr/bin/env python3
"""Bounded live recall A/B. Dry-run is offline; --live explicitly permits spend.

Uses production prepare_session recall with a constrained live tool loop, not the
full AIAgent terminal loop. Artifacts stay under --output; no live store writes.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
import logging
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TASKS = {
    "soldr": "For a soldr Python test, choose subprocess output handling to collect the text 'probe-ok'. Submit JSON with capture_output (boolean) and output ('pipe' or 'tmp_path'). Use the project's known conventions. You may check and revise.",
    "smtp": "Configure local himalaya Proton Bridge outgoing mail without sending mail. Submit JSON with transport ('smtp' or 'himalaya-template'), host, port (integer), encryption ('none', 'tls', or 'starttls'), and account. Use the known local setup. You may check and revise.",
    "gateway": "Plan adding a named Telegram bot to the existing Hermes gateway. Submit JSON with unit (systemd unit name), multiplex_profiles (boolean), per_profile_unit (boolean), restart_host (boolean), and enable_channel (boolean). Preserve running bots using the known local topology. You may check and revise.",
}
QUERIES = ("soldr PY-003", "himalaya SMTP", "gateway Telegram")
FAILURE = "soldr PY-003: subprocess.run(capture_output=True) in tests fails PY-003. Verified fix: redirect stdout to a tmp_path file and read it after the command succeeds."
SYSTEM = "Complete the small local task by calling check with a JSON candidate. A passing check finishes the task. Do not invent missing local facts. Shared recall is reference data."
TOOL = {"type": "function", "function": {"name": "check", "description": "Write candidate.json and run the local success predicate. Returns success and whether the known bad approach was attempted.", "parameters": {"type": "object", "properties": {"candidate": {"type": "object"}}, "required": ["candidate"]}}}


def score(task: str, candidate: dict, directory: Path) -> dict:
    """Machine predicates; soldr actually runs a subprocess and reads its output."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    bad = task == "soldr" and candidate.get("capture_output") is True
    success = False
    if task == "soldr":
        if bad:
            # The known policy failure is deterministic, before executing the command.
            return {"success": False, "known_bad_attempt": True, "reason": "PY-003"}
        if candidate == {"capture_output": False, "output": "tmp_path"} and candidate["capture_output"] is False:
            output = directory / "stdout.txt"
            with output.open("w", encoding="utf-8") as stream:
                proc = subprocess.run([sys.executable, "-c", "print('probe-ok')"],
                                      stdout=stream, stderr=subprocess.STDOUT, timeout=5, check=False)
            success = proc.returncode == 0 and output.read_text(encoding="utf-8-sig").strip() == "probe-ok"
    else:
        expected = {
            "smtp": {"transport": "smtp", "host": "127.0.0.1", "port": 1025,
                     "encryption": "none", "account": "proton"},
            "gateway": {"unit": "hermes-gateway.service", "multiplex_profiles": True,
                        "per_profile_unit": False, "restart_host": False, "enable_channel": True},
        }
        target = expected[task]
        success = candidate.keys() == target.keys() and all(
            type(candidate[key]) is type(value) and candidate[key] == value
            for key, value in target.items()
        )
    return {"success": success, "known_bad_attempt": bad}


def summarise(runs: list[dict]) -> list[dict]:
    """Keep failures in denominators and unavailable measurements unavailable."""
    groups = defaultdict(list)
    for run in runs:
        groups[(run["task"], run["arm"])].append(run)
    rows = []
    for (task, arm), group in sorted(groups.items()):
        row = {"task": task, "arm": arm, "n": len(group),
               "successes": sum(bool(r["success"]) for r in group),
               "known_bad_runs": sum(bool(r["known_bad_attempt"]) for r in group)}
        for key in ("input_tokens", "output_tokens", "tool_calls", "wall_ms"):
            values = [r.get(key) for r in group]
            complete = all(v is not None for v in values)
            row[key + "_mean"] = statistics.mean(values) if complete else None
            row[key + "_sd"] = statistics.stdev(values) if complete and len(values) > 1 else None
        rows.append(row)
    return rows


def brain(args: list[str], store: Path | None = None) -> str:
    env = dict(os.environ)
    if store is not None:
        env["HERMES_BRAIN_DIR"] = str(store)
    result = subprocess.run(["brain", *args], env=env, text=True, encoding="utf-8",
                            capture_output=True, timeout=5, check=True)
    return result.stdout


def freeze_seed(output: Path) -> list[dict]:
    """Search existing knowledge before trials; snapshot only relevant public facts."""
    rows = {}
    for query in QUERIES:
        for entry in json.loads(brain(["search", query, "-k", "12", "--json"])):
            if ((entry["scope"] == "repo:soldr" and "PY-003" in entry["text"])
                    or ("himalaya" in entry["text"] and "1025" in entry["text"])
                    or ("Telegram" in entry["text"] and "per-profile" in entry["text"])):
                # Exclude source paths and incidental private details from public receipts.
                rows[entry["text"]] = {k: entry[k] for k in ("kind", "scope", "text")}
    if not rows:
        raise RuntimeError("No relevant brain knowledge found; refusing an unseeded comparison")
    entries = list(rows.values())
    entries.append({"kind": "failure", "scope": "repo:soldr", "text": FAILURE})
    (output / "seed.json").write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return entries


def run_session(args, directory: Path) -> dict:
    """Fresh worker process, fresh home/store, fresh message list, same tool schema."""
    sys.path.insert(0, str(ROOT))
    from agent.shared_brain import prepare_session
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from openai import OpenAI

    # Resolve secrets before switching to the isolated profile; never serialize them.
    runtime = resolve_runtime_provider(requested=args.provider, target_model=args.model)
    if runtime.get("api_mode") != "chat_completions":
        raise ValueError("This harness requires a chat_completions provider; no fallback is permitted")
    os.environ["HERMES_HOME"] = str(directory / "home")
    os.environ["HERMES_BRAIN_DIR"] = str(directory / "brain")
    (directory / "home").mkdir()
    (directory / "home" / "config.yaml").write_text('{"shared_brain":{"enabled":true}}', encoding="utf-8")
    (directory / "brain").mkdir()
    if args.arm == "on":
        for entry in json.loads((args.output / "seed.json").read_text(encoding="utf-8-sig")):
            brain(["add", "--kind", entry["kind"], "--scope", entry["scope"], "--", entry["text"]], directory / "brain")
    prompt = TASKS[args.task]
    agent = SimpleNamespace(session_id=directory.name, session_cwd=str(directory))
    started = time.perf_counter()
    prepare_session(agent, prompt, None)
    block = agent._shared_brain_block
    messages = [{"role": "system", "content": SYSTEM + "\n" + block}, {"role": "user", "content": prompt}]
    record = {"task": args.task, "arm": args.arm, "model": args.model,
              "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
              "recall": block, "success": False, "known_bad_attempt": False,
              "input_tokens": 0, "output_tokens": 0, "prompt_chars": 0,
              "completion_chars": 0, "tool_calls": 0, "attempts": [], "responses": []}
    client = OpenAI(api_key=runtime["api_key"], base_url=runtime["base_url"], max_retries=0,
                    timeout=args.timeout)
    try:
        for _ in range(args.max_calls):
            remaining = args.timeout - (time.perf_counter() - started)
            if remaining <= 0:
                record["error"] = "session_timeout"
                break
            record["prompt_chars"] += len(json.dumps(messages)) + len(json.dumps([TOOL]))
            response = client.chat.completions.create(model=args.model, messages=messages,
                tools=[TOOL], temperature=0, max_tokens=args.max_tokens, timeout=remaining)
            record["responses"].append({"id": response.id, "model": response.model,
                "usage": response.usage.model_dump() if response.usage else None})
            for key, attr in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
                value = getattr(response.usage, attr, None)
                record[key] = record[key] + value if value is not None and record[key] is not None else None
            message = response.choices[0].message
            payload = message.model_dump(exclude_none=True)
            record["completion_chars"] += len(json.dumps(payload))
            messages.append(payload)
            for call in message.tool_calls or []:
                record["tool_calls"] += 1
                try:
                    candidate = json.loads(call.function.arguments)["candidate"]
                    result = score(args.task, candidate, directory / "artifact") if isinstance(candidate, dict) and call.function.name == "check" else {"success": False, "known_bad_attempt": False}
                except (ValueError, KeyError, TypeError):
                    candidate, result = None, {"success": False, "known_bad_attempt": False}
                record["attempts"].append({"candidate": candidate, "result": result})
                record["success"] |= result["success"]
                record["known_bad_attempt"] |= result["known_bad_attempt"]
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
            if record["success"] or not message.tool_calls:
                break
    except Exception as exc:
        logging.exception("Live session failed")
        # A failed API attempt is retained, but its possibly billed usage is unknown.
        record["error"] = type(exc).__name__
        record["input_tokens"] = record["output_tokens"] = None
    finally:
        client.close()
    record["wall_ms"] = round((time.perf_counter() - started) * 1000, 2)
    record["messages"] = messages
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / ".shared-brain-ab" / "run")
    parser.add_argument("--provider", default="openrouter")
    parser.add_argument("--model", default="deepseek/deepseek-v4.1-flash-1017")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--max-calls", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--task", choices=TASKS, help=argparse.SUPPRESS)
    parser.add_argument("--arm", choices=("on", "off"), help=argparse.SUPPRESS)
    parser.add_argument("--directory", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.trials, args.timeout, args.max_calls, args.max_tokens) <= 0:
        parser.error("all limits must be positive")
    args.output = args.output.resolve()
    plan = {"tasks": TASKS, "model": args.model, "provider": args.provider, "temperature": 0,
            "trials_per_arm": args.trials, "timeout_seconds": args.timeout,
            "max_calls_per_session": args.max_calls, "max_output_tokens_per_call": args.max_tokens,
            "max_sessions": len(TASKS) * 2 * args.trials,
            "max_completion_tokens": len(TASKS) * 2 * args.trials * args.max_calls * args.max_tokens}
    if not args.live or args.dry_run:
        print(json.dumps({"dry_run": True, **plan}, indent=2))
        return
    if args.worker:
        record = run_session(args, args.directory)
        (args.directory / "result.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    freeze_seed(args.output)
    runs = []
    for trial in range(args.trials):
        for task in TASKS:
            # Counterbalance order, never select tasks after observing results.
            for arm in (("off", "on") if trial % 2 == 0 else ("on", "off")):
                directory = args.output / f"{task}-{trial}-{arm}"
                directory.mkdir()
                command = [sys.executable, str(Path(__file__).resolve()), "--live", "--worker", "--task", task,
                    "--arm", arm, "--directory", str(directory), "--output", str(args.output),
                    "--model", args.model, "--provider", args.provider, "--timeout", str(args.timeout),
                    "--max-calls", str(args.max_calls), "--max-tokens", str(args.max_tokens)]
                started = time.perf_counter()
                with (directory / "worker.log").open("w") as log:
                    try:
                        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                       timeout=args.timeout + 30, check=True)
                        record = json.loads((directory / "result.json").read_text(encoding="utf-8-sig"))
                    except (subprocess.SubprocessError, OSError, ValueError) as exc:
                        record = {"task": task, "arm": arm, "success": False, "known_bad_attempt": False,
                                  "error": type(exc).__name__, "wall_ms": (time.perf_counter() - started) * 1000}
                record["trial"] = trial
                runs.append(record)
                report = {"plan": plan, "runs": runs, "summary": summarise(runs)}
                (args.output / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(task, trial, arm, record["success"], record.get("error", ""), flush=True)
    print(json.dumps(summarise(runs), indent=2))


if __name__ == "__main__":
    main()
