"""``/workflow`` — saved multi-step prompts, run as one normal turn in the current chat.

A workflow is a YAML file in ``<HERMES_HOME>/workflows/<name>.yaml``::

    name: release-notes
    description: Draft release notes from recent commits
    steps:
      - name: Gather
        prompt: List the commits since the last tag.
      - name: Draft
        prompt: Write release notes for {input}.

``{input}`` in a step is replaced with the text typed after ``/workflow <name>``; when no
step uses it, that text is passed along as the workflow's input. Like ``/plan``, every
surface feeds :func:`build_workflow_prompt` to the live agent as a normal user turn: no
engine, no model-tool footprint, system prompt and history untouched (prompt-cache safe).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from hermes_constants import get_hermes_home

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_STEPS = 50


class WorkflowError(ValueError):
    """A workflow name or document that cannot be loaded, saved or run."""


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    prompt: str


@dataclass(frozen=True)
class Workflow:
    name: str
    description: str
    steps: tuple[WorkflowStep, ...]


def workflows_dir() -> Path:
    return get_hermes_home() / "workflows"


def _check_name(name: str) -> str:
    clean = (name or "").strip().lower()
    if not _NAME_RE.match(clean):
        raise WorkflowError(f"invalid workflow name {name!r}: use lowercase letters, digits, '-' or '_'")
    return clean


def _text(value: object, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, (str, int, float)):
        raise WorkflowError(f"{field} must be text")
    return str(value).strip()


def parse_workflow(text: str, name: str) -> Workflow:
    """Validate a workflow YAML document; ``name`` is the file stem (it wins over ``name:``)."""
    from hermes_yaml import YAMLError, safe_load

    try:
        data = safe_load(text) or {}
    except YAMLError as exc:
        raise WorkflowError(f"workflow {name!r} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"workflow {name!r} must be a mapping with a 'steps' list")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowError(f"workflow {name!r} needs a non-empty 'steps' list")
    if len(raw_steps) > MAX_STEPS:
        raise WorkflowError(f"workflow {name!r} has more than {MAX_STEPS} steps")
    steps = []
    for index, raw in enumerate(raw_steps, 1):
        step = {"prompt": raw} if isinstance(raw, str) else raw
        if not isinstance(step, dict):
            raise WorkflowError(f"step {index} must be text or a mapping with a 'prompt'")
        prompt = _text(step.get("prompt"), f"step {index} prompt")
        if not prompt:
            raise WorkflowError(f"step {index} has no prompt")
        steps.append(WorkflowStep(name=_text(step.get("name"), f"step {index} name") or f"Step {index}",
                                  prompt=prompt))
    return Workflow(name=name, description=_text(data.get("description"), "description"), steps=tuple(steps))


def load_workflow(name: str) -> Workflow:
    clean = _check_name(name)
    path = workflows_dir() / f"{clean}.yaml"
    if not path.is_file():
        raise WorkflowError(f"no workflow named {clean!r} in {workflows_dir()}")
    return parse_workflow(path.read_text(encoding="utf-8-sig"), clean)


def list_workflows() -> list[dict]:
    """Every saved workflow as ``{name, description, steps, source, error}``, sorted by name.

    A file that does not parse is still listed (with ``error``) so the editor can open and fix it.
    """
    root = workflows_dir()
    rows = []
    for path in sorted(root.glob("*.yaml")) if root.is_dir() else []:
        if not _NAME_RE.match(path.stem):
            continue
        source = path.read_text(encoding="utf-8-sig")
        try:
            wf = parse_workflow(source, path.stem)
            rows.append({"name": wf.name, "description": wf.description, "steps": len(wf.steps),
                         "source": source, "error": None})
        except WorkflowError as exc:
            rows.append({"name": path.stem, "description": "", "steps": 0, "source": source, "error": str(exc)})
    return rows


def save_workflow(name: str, source: str) -> Workflow:
    """Validate then atomically write ``source``; an invalid document is never written."""
    clean = _check_name(name)
    wf = parse_workflow(source, clean)
    root = workflows_dir()
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".{clean}.yaml.tmp"
    tmp.write_text(source if source.endswith("\n") else source + "\n", encoding="utf-8")
    tmp.replace(root / f"{clean}.yaml")
    return wf


def split_workflow_args(arg: str) -> tuple[str, str]:
    """``"<name> rest of prompt"`` → ``(name, prompt)``."""
    parts = (arg or "").strip().split(None, 1)
    return (parts[0] if parts else ""), (parts[1].strip() if len(parts) > 1 else "")


def build_workflow_prompt(wf: Workflow, user_input: str = "") -> str:
    user_input = (user_input or "").strip()
    uses_input = any("{input}" in s.prompt for s in wf.steps)
    lines = [f"[/workflow {wf.name}]", ""]
    if wf.description:
        lines += [f"Workflow: {wf.description}", ""]
    lines += [
        f"Run this {len(wf.steps)}-step workflow now, in order. For each step, start a section headed "
        "`## Step N: <name>`, do the work with the tools you need, and carry its results into the "
        "next step. Do not skip steps or stop to ask unless a step cannot be done; if one fails, say "
        "why under its heading and continue when later steps can still run. Finish with a section "
        "headed `## Workflow complete` that summarizes the outcome of every step.",
        "",
    ]
    if user_input and not uses_input:
        lines += ["Input:", user_input, ""]
    for index, step in enumerate(wf.steps, 1):
        lines += [f"Step {index}: {step.name}", step.prompt.replace("{input}", user_input or "(no input given)"), ""]
    return "\n".join(lines).rstrip() + "\n"


def workflow_usage() -> str:
    names = [row["name"] for row in list_workflows()]
    listing = ", ".join(names) if names else f"none yet; add YAML files to {workflows_dir()}"
    return f"usage: /workflow <name> [input]\nworkflows: {listing}"


def resolve_workflow_command(arg: str) -> tuple[str | None, str]:
    """Shared by every surface: ``(prompt, "")`` to submit, or ``(None, text)`` to show the user."""
    name, user_input = split_workflow_args(arg)
    if not name:
        return None, workflow_usage()
    try:
        return build_workflow_prompt(load_workflow(name), user_input), ""
    except WorkflowError as exc:
        return None, f"{exc}\n{workflow_usage()}"
