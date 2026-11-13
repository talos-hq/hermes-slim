"""Saved-workflow RPCs for the desktop editor (``agent/workflow.py`` owns the format).
Bodies are rebound onto server.py's globals at install (method_ctx.bind_module)."""

from __future__ import annotations

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method


@method("workflow.list")
@_registry.profile_scoped
def _workflow_list(rid, params: dict) -> dict:
    from agent import workflow as wf
    return _ok(rid, {"workflows": wf.list_workflows(), "dir": str(wf.workflows_dir())})


@method("workflow.save")
@_registry.profile_scoped
def _workflow_save(rid, params: dict) -> dict:
    from agent import workflow as wf
    try:
        saved = wf.save_workflow(str(params.get("name") or ""), str(params.get("source") or ""))
    except wf.WorkflowError as exc:
        return _err(rid, 4004, str(exc))
    return _ok(rid, {"name": saved.name, "steps": len(saved.steps)})


def register(server) -> None:
    """Publish this module's handlers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
