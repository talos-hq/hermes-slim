"""Offline behavior contracts for the live A/B evaluator."""
import json

import pytest

from scripts.shared_brain_ab import main, score, summarise


def test_scoring_verifies_output_and_retains_failure(tmp_path):
    bad = score("soldr", {"capture_output": True, "output": "pipe"}, tmp_path)
    assert bad == {"success": False, "known_bad_attempt": True, "reason": "PY-003"}
    good = score("soldr", {"capture_output": False, "output": "tmp_path"}, tmp_path)
    assert good == {"success": True, "known_bad_attempt": False}
    assert (tmp_path / "stdout.txt").read_text(encoding="utf-8-sig").strip() == "probe-ok"
    assert json.loads((tmp_path / "candidate.json").read_text(encoding="utf-8-sig"))["output"] == "tmp_path"


def test_config_predicates_reject_wrong_local_settings(tmp_path):
    smtp = {"transport": "smtp", "host": "127.0.0.1", "port": 1025,
            "encryption": "none", "account": "proton"}
    gateway = {"unit": "hermes-gateway.service", "multiplex_profiles": True,
               "per_profile_unit": False, "restart_host": False, "enable_channel": True}
    for task, candidate, wrong in (("smtp", smtp, {"port": 587}),
                                   ("gateway", gateway, {"restart_host": True})):
        assert score(task, candidate, tmp_path)["success"]
        assert not score(task, {**candidate, **wrong}, tmp_path)["success"]
    assert not score("gateway", {**gateway, "multiplex_profiles": 1}, tmp_path)["success"]


def test_summary_keeps_failures_and_unknown_usage():
    runs = [
        {"task": "soldr", "arm": "on", "success": True, "known_bad_attempt": False,
         "input_tokens": 10, "output_tokens": 2, "tool_calls": 1, "wall_ms": 100},
        {"task": "soldr", "arm": "on", "success": False, "known_bad_attempt": True,
         "input_tokens": None, "output_tokens": None, "tool_calls": 3, "wall_ms": 300},
    ]
    row, = summarise(runs)
    assert (row["n"], row["successes"], row["known_bad_runs"]) == (2, 1, 1)
    assert row["input_tokens_mean"] is None
    assert row["output_tokens_sd"] is None
    assert row["tool_calls_mean"] == 2
    assert round(row["wall_ms_sd"], 2) == 141.42
    single, = summarise(runs[:1])
    assert single["wall_ms_sd"] is None
    assert summarise([]) == []


@pytest.mark.parametrize("flags", [[], ["--dry-run"], ["--live", "--dry-run"], ["--worker", "--dry-run"]])
def test_dry_run_never_resolves_credentials_or_calls_provider(monkeypatch, capsys, flags):
    monkeypatch.setattr("sys.argv", ["shared_brain_ab.py", *flags])
    monkeypatch.setattr("scripts.shared_brain_ab.freeze_seed", lambda *_: (_ for _ in ()).throw(
        AssertionError("dry-run touched brain")))
    monkeypatch.setattr("scripts.shared_brain_ab.run_session", lambda *_: (_ for _ in ()).throw(
        AssertionError("dry-run called provider")))
    main()
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True
    assert plan["max_sessions"] == len(plan["tasks"]) * 2 * plan["trials_per_arm"]
