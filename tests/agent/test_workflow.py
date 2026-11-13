"""/workflow: saved YAML workflows become one normal user turn (agent/workflow.py)."""
import pytest

from agent import workflow as wf

GOOD = """description: Two steps
steps:
  - name: Greet
    prompt: Greet {input}.
  - Sign off politely.
"""


def test_saved_workflow_lists_and_builds_an_ordered_prompt_with_input():
    wf.save_workflow("hello", GOOD)
    [row] = wf.list_workflows()
    assert (row["name"], row["steps"], row["error"]) == ("hello", 2, None)
    prompt, message = wf.resolve_workflow_command("hello the team")
    assert message == ""
    assert prompt.index("Step 1: Greet") < prompt.index("Greet the team.") < prompt.index("Step 2: Step 2")
    assert "Input:" not in prompt  # {input} consumed it


def test_input_without_placeholder_is_passed_as_workflow_input():
    wf.save_workflow("plain", "steps: [Summarize the input.]")
    prompt, _ = wf.resolve_workflow_command("plain the README")
    assert "Input:\nthe README" in prompt


@pytest.mark.parametrize("source", ["steps: []", "steps: [{name: x}]", "- not a mapping", "steps: [a"])
def test_invalid_workflow_is_refused_and_never_written(source):
    with pytest.raises(wf.WorkflowError):
        wf.save_workflow("bad", source)
    assert not (wf.workflows_dir() / "bad.yaml").exists()


@pytest.mark.parametrize("name", ["../escape", ".hidden", "", "a/b"])
def test_names_cannot_leave_the_workflows_dir(name):
    with pytest.raises(wf.WorkflowError):
        wf.save_workflow(name, GOOD)


def test_unknown_or_missing_name_answers_usage_not_a_prompt():
    wf.save_workflow("hello", GOOD)
    for arg in ("", "nope"):
        prompt, message = wf.resolve_workflow_command(arg)
        assert prompt is None and "usage: /workflow" in message and "hello" in message


def test_workflows_live_under_the_active_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other"))
    assert wf.workflows_dir() == tmp_path / "other" / "workflows"
