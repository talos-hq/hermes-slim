"""Compact delegation outcomes through the same CLI capture adapter as parent tasks."""

import json
import logging
from pathlib import Path
import re

from agent.shared_brain import MAX_SCOPE_CHARS, MAX_WRITE_CHARS, record_outcome
from tools.brain_tool import brain_enabled

logger = logging.getLogger(__name__)
# Explicit lesson lines avoid persisting whole task reports or guessing that completion
# means a fix. The child can supply these without another model call.
_SOLUTION = re.compile(r"^\s*(?:[-*]\s*)?(?:Solution|Fix|Verified command):\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_CONFIRMED_FIX = re.compile(
    r"^\s*(?:[-*]\s*)?((?:Fixed|Resolved)\b[^\n]*(?:\bverified\b|\btested\b|\bpassed\b)[^\n]*)$",
    re.IGNORECASE | re.MULTILINE,
)
_UNKNOWN_FAILURE = "Subagent did not produce a response."


def _identified_solution(entry: dict) -> str:
    summary = str(entry.get("summary") or "")
    match = _SOLUTION.search(summary) or _CONFIRMED_FIX.search(summary)
    if match:
        return match.group(1)
    if entry.get("schema_valid") is True:
        from tools.delegation_output_schema import extract_json_candidate
        candidate = extract_json_candidate(summary)
        if candidate:
            output = json.loads(candidate)
            if isinstance(output, dict):
                for key in ("solution", "fix", "verified_command"):
                    value = output.get(key)
                    if isinstance(value, str) and value.strip():
                        return value
    return ""


def capture_delegation_outcome(entry: dict, child) -> None:
    """Best effort, no model call, no second store, no changes to the child's result."""
    try:
        if not brain_enabled():
            return
        failure, solution = "", ""
        if entry.get("status") in {"failed", "error", "timeout"}:
            reason = entry.get("failure_reason") or entry.get("error")
            if isinstance(reason, str) and reason.strip() and reason != _UNKNOWN_FAILURE:
                failure = reason
        elif entry.get("status") == "completed" and entry.get("exit_reason") == "completed":
            solution = _identified_solution(entry)
        if not failure and not solution:
            return
        from tools.delegation_redaction import redact_child_text
        scope = "repo:" + Path(getattr(child, "session_cwd", None) or ".").resolve().name
        record_outcome(
            redact_child_text(failure, force=True)[:MAX_WRITE_CHARS],
            redact_child_text(solution, force=True)[:MAX_WRITE_CHARS],
            scope=scope[:MAX_SCOPE_CHARS],
        )
    except Exception as exc:  # health: allow BLE001 -- optional persistence must never abort delegation; exception text can contain secrets
        logger.debug("Delegation brain capture unavailable (%s)", type(exc).__name__)
