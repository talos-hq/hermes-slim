"""Saved workflows (``workflow.*``): the desktop editor's read/write door onto
``<HERMES_HOME>/workflows/*.yaml`` (``agent/workflow.py``). Running one is ``/workflow``."""

from __future__ import annotations

from .base import Result
from .common import ProfileParams
from .registry import method


class WorkflowRow(Result):
    name: str
    description: str = ""
    steps: int = 0
    source: str
    error: str | None = None


class WorkflowListResult(Result):
    workflows: list[WorkflowRow]
    dir: str


class WorkflowSaveParams(ProfileParams):
    name: str
    source: str


class WorkflowSaveResult(Result):
    name: str
    steps: int


method("workflow.list", params=ProfileParams, result=WorkflowListResult,
       doc="Every saved workflow of the profile, with its YAML source (unparseable files carry error).")
method("workflow.save", params=WorkflowSaveParams, result=WorkflowSaveResult,
       doc="Validate and write one workflow; an invalid document is refused (4004) and never written.")
