#!/usr/bin/env python3
"""Fail if the fork's old "hermes-slim" branding (or a mangled Talos name) is back.

The fork is branded Talos. Upstream Hermes names (`hermes`, `hermes_*`, ~/.hermes,
"Hermes Agent") are untouched on purpose and are not checked here.

    python slim/lint_branding.py        # exit 1 and file:line: text for each hit

Scans `git ls-files`. Allowed legacy text is listed in ALLOWED below, each with why.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

LEGACY = re.compile(r"hermes[-_ ]?slim", re.IGNORECASE)  # also covers HERMES_SLIM(_DIR)
BAD_TALOS = re.compile(r"talos[-_ ]?slim|hermes[-_ ]?talos", re.IGNORECASE)

# Text removed from every line before checking: external names we do not control.
ALLOWED_EVERYWHERE = [
    ("talos-hq/hermes-slim", "the GitHub repo slug; renaming the repo is separate (GitHub redirects)"),
    ("hermes-slim/wheels", "slug of the separate wheels repo, which keeps its name"),
]

# (path, line regex, why): a line in that file matching the regex may say the legacy name.
ALLOWED = [
    ("slim/lint_branding.py", r".", "this lint names the legacy patterns it looks for"),
    ("slim/tests/test_lint_branding.py", r".", "the lint's test feeds it legacy text"),
    ("plugins/model-providers/nous/__init__.py", r'environ\.get\("HERMES_SLIM"\)',
     "compat: installs set up before the rename have HERMES_SLIM=1 in ~/.hermes/.env"),
    ("hermes_cli/setup.py", r'environ\.get\("HERMES_SLIM"\)', "compat: legacy HERMES_SLIM env var"),
    ("slim/install.sh", r"HERMES_SLIM_DIR", "compat: legacy install-dir override"),
    ("slim/install.sh", r"\.hermes-slim|hermes-slim-setup|# hermes-slim|legacy prefix|prefix in talos hermes-slim",
     "compat: migrate ~/.hermes-slim, its launcher link and PATH line, and old-prefixed release assets"),
    ("slim/install.ps1", r'"hermes-slim"|hermes-slim-\*|legacy', "compat: migrate %LOCALAPPDATA%\\hermes-slim and old assets"),
]


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


def check_text(path: str, text: str) -> list[str]:
    rules = [re.compile(rx) for p, rx, _ in ALLOWED if p == path]
    exempt = [p for p, rx, _ in ALLOWED if rx == "."]
    if path in exempt:
        return []
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line
        for token, _ in ALLOWED_EVERYWHERE:
            stripped = stripped.replace(token, "")
        if BAD_TALOS.search(stripped):
            problems.append(f"{path}:{n}: malformed Talos name: {line.strip()}")
        elif LEGACY.search(stripped) and not any(r.search(line) for r in rules):
            problems.append(f"{path}:{n}: legacy hermes-slim branding: {line.strip()}")
    return problems


def main(argv=None) -> int:
    problems = []
    for path in tracked_files():
        try:
            text = (ROOT / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # binary, symlink to nowhere, or deleted in the work tree
        problems += check_text(path, text)
    for p in problems:
        print(p)
    if problems:
        print(f"\n{len(problems)} branding problem(s); the fork is 'Talos' (see slim/lint_branding.py)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
