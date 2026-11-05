"""Subagent-safe shared lessons; profile MEMORY.md remains a separate capability."""

import json
import logging

from agent.shared_brain import KINDS, MAX_SCOPE_CHARS, MAX_WRITE_CHARS, add_entry
from hermes_cli.config import load_config_readonly
from hermes_platform.resolver.core import locate_command
from tools.registry import registry

logger = logging.getLogger(__name__)


def brain_enabled() -> bool:
    config = load_config_readonly().get("shared_brain") or {}
    return isinstance(config, dict) and config.get("enabled", True) is not False and bool(locate_command("brain").command)


def brain_add(args: dict) -> str:
    """Return an ordinary tool error on unavailable/slow/invalid writes, never stop a task."""
    try:
        if not brain_enabled():
            return json.dumps({"success": False, "error": "Shared brain is disabled or unavailable"})
        add_entry(args.get("kind", "fact"), args.get("scope", ""), args.get("text", ""))
        return json.dumps({"success": True})
    except Exception as exc:  # health: allow BLE001 -- optional persistence must never abort delegation; exception text can contain secrets
        # Do not echo CLI output or submitted text: either may contain private data.
        logger.debug("Shared brain append unavailable (%s)", type(exc).__name__)
        return json.dumps({"success": False, "error": f"Brain append failed ({type(exc).__name__}); continue the task"})


registry.register(
    name="brain_add", toolset="brain", handler=brain_add, check_fn=brain_enabled,
    schema={
        "name": "brain_add",
        "description": "Append a concise reusable fact, confirmed failure or verified solution to the shared brain. "
                       "Never store secrets, raw output, personal data, binary content or guesses. "
                       "Duplicate entries collapse. Failure is non-fatal; continue your task.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(KINDS)},
                "scope": {"type": "string", "maxLength": MAX_SCOPE_CHARS, "description": "Knowledge scope, e.g. repo:widget"},
                "text": {"type": "string", "minLength": 1, "maxLength": MAX_WRITE_CHARS},
            },
            "required": ["kind", "scope", "text"],
            "additionalProperties": False,
        },
    },
)
