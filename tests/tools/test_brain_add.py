"""Delegated discoveries persist through the operator-inspectable brain CLI."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.shared_brain import prepare_session, retrieve
from hermes_platform.resolver.core import locate_command
from tools.registry import registry


@pytest.fixture
def brain(tmp_path, monkeypatch):
    if not locate_command("brain").command:
        pytest.skip("requires the separately installed brain CLI")
    monkeypatch.setenv("HERMES_BRAIN_DIR", str(tmp_path / "brain"))
    import tools.brain_tool  # register the real handler
    monkeypatch.setattr("agent.shared_brain.TIMEOUT", 2)
    return tmp_path


def add(text, **kwargs):
    return json.loads(registry.dispatch("brain_add", {"kind": "fact", "scope": "repo:widget", "text": text, **kwargs}))


def test_child_write_reaches_next_parent_snapshot_and_memory_stays_blocked(brain):
    from tools.delegate_tool_toolsets import _resolve_child_toolsets
    from toolsets import resolve_toolset

    parent = SimpleNamespace(enabled_toolsets=["hermes-cli"], disabled_toolsets=[], session_id="parent")
    enabled, disabled = _resolve_child_toolsets(parent, None, "leaf")
    names = set().union(*(resolve_toolset(t) for t in enabled))
    names -= set().union(*(resolve_toolset(t) for t in disabled))
    assert "brain_add" in names
    assert "memory" not in names
    with patch("hermes_cli.config.load_config_readonly", return_value={}):
        prepare_session(parent, "Widget compilation", None)
        frozen = parent._shared_brain_block
        assert finish({"status": "completed", "exit_reason": "completed", "summary": "Recorded discovery"},
                      lambda: add("Widget compilation requires the generated include directory"))["status"] == "completed"
        prepare_session(parent, "Widget compilation", [])
        assert parent._shared_brain_block == frozen
        parent.session_id = "parent-next"
        prepare_session(parent, "Widget compilation", [])
    assert "generated include directory" in parent._shared_brain_block


@pytest.mark.parametrize("cold_start", range(4))
def test_eight_parallel_child_writers_and_duplicates(brain, cold_start):
    barrier = threading.Barrier(8)

    def write(index):
        def discover():
            barrier.wait(timeout=10)
            text = f"Widget parallel discovery number {index}"
            assert add(text)["success"]
            return add(text.upper())
        result = finish({"status": "completed", "exit_reason": "completed", "summary": "Recorded discovery"}, discover)
        assert result["status"] == "completed", result

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(8)))
    with sqlite3.connect(brain / "brain" / "brain.sqlite") as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("SELECT count(*) FROM entries").fetchone()[0] == 8
    block = retrieve("Widget parallel discovery", str(brain))
    assert all(f"number {i}" in block for i in range(8))


@pytest.mark.parametrize("changes", [
    {"text": "x" * 2049}, {"text": b"binary"}, {"text": "bad\x00data"},
    {"text": "bad\x1bdata"}, {"kind": "unknown"}, {"scope": "x" * 129},
    {"text": " "}, {"text": "\ud800"},
])
def test_rejects_unbounded_or_nontext_entries(brain, changes):
    args = {"kind": "fact", "scope": "repo:widget", "text": "Widget lesson", **changes}
    assert not json.loads(registry.dispatch("brain_add", args))["success"]
    assert not (brain / "brain" / "brain.sqlite").exists()


def test_cli_failure_is_a_nonfatal_tool_result(brain):
    def discover():
        with patch("agent.shared_brain.subprocess.run", side_effect=OSError("offline")):
            result = add("Widget lesson")
        assert result["success"] is False
        assert "error" in result
        return {"success": True}

    result = finish({"status": "completed", "exit_reason": "completed", "summary": "Task completed"}, discover)
    assert result["status"] == "completed"


def finish(entry, discover=None):
    from tools.delegate_tool_results import _run_child_lifecycle

    def converse(**kwargs):
        if discover:
            assert discover()["success"]
        return {"final_response": entry.get("summary", ""),
                "completed": entry.get("exit_reason") == "completed",
                "failed": entry["status"] == "failed", "error": entry.get("error", ""),
                "interrupted": entry["status"] == "interrupted", "messages": []}

    child = SimpleNamespace(session_id="child", session_cwd="/repo/widget", tool_progress_callback=None,
                            _credential_pool=None, run_conversation=converse, close=lambda: None)
    if "schema_valid" in entry:
        child._delegate_output_schema = {"type": "object", "properties": {"solution": {"type": "string"}},
                                         "required": ["solution"]}
    return _run_child_lifecycle(0, "Fix widget compilation", child, None)


@pytest.mark.parametrize("entry,kind,lesson", [
    ({"status": "completed", "exit_reason": "completed", "summary": "Solution: Added widget include directory; verified with `make widget`."},
     "solution", "Added widget include directory"),
    ({"status": "completed", "exit_reason": "completed", "summary": "Fixed widget compilation by adding include directory; verified with make widget."},
     "solution", "Fixed widget compilation"),
    ({"status": "completed", "exit_reason": "completed", "schema_valid": True,
      "summary": '{"solution": "Added widget include directory; verified make widget"}'},
     "solution", "Added widget include directory"),
    ({"status": "failed", "error": "Widget compiler cannot find widget.h", "summary": ""},
     "failure", "Widget compiler cannot find widget.h"),
])
def test_delegation_end_reuses_outcome_capture(brain, entry, kind, lesson):
    result = finish(entry)
    assert result["status"] == entry["status"]
    block = retrieve("Widget", str(brain))
    assert f'"kind": "{kind}"' in block
    assert lesson in block


@pytest.mark.parametrize("entry", [
    {"status": "completed", "exit_reason": "completed", "summary": "Solution: Fixed widget with `make widget`."},
    {"status": "failed", "summary": "", "error": "Widget compiler unavailable"},
])
def test_capture_failure_does_not_change_child_result(brain, entry):
    with patch("agent.shared_brain.subprocess.run", side_effect=OSError("offline")):
        result = finish(entry.copy())
    assert result["status"] == entry["status"]
    assert result["summary"] == entry["summary"]


@pytest.mark.parametrize("entry", [
    {"status": "completed", "exit_reason": "completed", "summary": "I reviewed the widget."},
    {"status": "completed", "exit_reason": "max_iterations", "summary": "Solution: Try a widget rebuild."},
    {"status": "interrupted", "summary": "Solution: Try a widget rebuild."},
    {"status": "failed", "summary": "", "error": "Subagent did not produce a response."},
])
def test_no_inferred_solution_or_unknown_failure(brain, entry):
    finish(entry)
    assert not (brain / "brain" / "brain.sqlite").exists()


@pytest.mark.parametrize("stderr,expected_calls", [
    ("sqlite3.OperationalError: database is locked", 2),
    ("sqlite3.OperationalError: disk I/O error", 1),
])
def test_retry_only_transient_cli_lock_failure(brain, stderr, expected_calls):
    error = subprocess.CalledProcessError(1, ["brain", "add"], stderr=stderr)
    with patch("agent.shared_brain.subprocess.run", side_effect=[error, subprocess.CompletedProcess([], 0)]) as run:
        result = add("Widget lesson")
    assert run.call_count == expected_calls
    assert result["success"] is (expected_calls == 2)
    if expected_calls == 2:
        assert run.call_args_list[0].args == run.call_args_list[1].args
