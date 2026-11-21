#!/usr/bin/env python3
"""Computer use through Hermes' own tool layer, against a real cua-driver.

Run by slim/smoke_test.py with the bundle's interpreter (PYTHONPATH=app), on a display it
prepared (Xvfb plus one X client on CI). Asserts, in order:
  1. the driver Hermes resolves is the one baked into the bundle and meets the runtime contract,
  2. the computer_use toolset is admitted (check_fn) and auto-approves by default,
  3. ``list_windows`` returns the window the caller started (``--expect-window``),
  4. ``capture`` (vision) returns a screenshot of it (inline image or the persisted file).
Prints one JSON summary line. Exit 0 = pass. No model or provider key is involved.

    python slim/computer_use_smoke.py --bundle <hermes-slim dir> [--expect-window TITLE] [--contract-only]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _call(action: str, **args) -> dict | list:
    from tools.computer_use.tool import handle_computer_use

    out = handle_computer_use({"action": action, **args}, session_id="slim-smoke")
    return json.loads(out) if isinstance(out, str) else out


def _app_of(listing, title: str):
    """App name of the listed window whose title contains ``title`` (capture targets apps, not titles)."""
    stack = [listing]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if title in str(node.get("title") or node.get("window_title") or ""):
                return node.get("app_name") or node.get("app") or node.get("name")
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _screenshot(result) -> str:
    """Where a capture's screenshot went: the inline image of a multimodal result (model reads images), or the
    shareable file every image capture is persisted to (text / auxiliary-vision results). ``""`` when none."""
    if isinstance(result, dict) and result.get("_multimodal"):
        parts = [p for p in result.get("content") or [] if isinstance(p, dict) and p.get("type") == "image_url"]
        return "inline image" if parts else ""
    path = result.get("screenshot_path") if isinstance(result, dict) else None
    return path if path and Path(path).is_file() and Path(path).stat().st_size > 0 else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", type=Path, required=True)
    ap.add_argument("--expect-window", default="")
    ap.add_argument("--contract-only", action="store_true", help="stop after the driver/contract checks")
    args = ap.parse_args()
    summary: dict = {}

    from tools.computer_use.cua_backend_driver import (
        bundled_cua_driver_cmd, cua_driver_runtime_contract_status, resolve_cua_driver_cmd)

    driver = resolve_cua_driver_cmd()
    bundled = bundled_cua_driver_cmd()
    summary["driver"] = driver
    assert driver and bundled == driver, f"resolved {driver!r}, bundled {bundled!r}"
    assert Path(driver).resolve().is_relative_to((args.bundle / "tools" / "cua-driver").resolve()), driver
    contract = cua_driver_runtime_contract_status(driver)
    summary["version"] = contract.get("version")
    assert contract.get("ready"), contract

    from tools.computer_use.cua_backend import cua_auto_approve
    from tools.computer_use.tool import check_computer_use_requirements

    assert check_computer_use_requirements(), "computer_use toolset not admitted"
    assert cua_auto_approve(), "computer_use.auto_approve should default to true"
    if args.contract_only:
        print(json.dumps({"ok": True, **summary}))
        return 0

    try:
        windows = _call("list_windows")
        text = json.dumps(windows)
        summary["list_windows_bytes"] = len(text)
        assert "error" not in windows or not windows.get("error"), windows
        app = None
        if args.expect_window:
            assert args.expect_window in text, f"{args.expect_window!r} not in list_windows: {text[:2000]}"
            app = _app_of(windows, args.expect_window)
            summary["app"] = app
        capture = _call("capture", mode="vision", app=app)
        shot = _screenshot(capture)
        summary["screenshot"] = shot
        assert shot, f"capture returned no screenshot: {json.dumps(capture)[:2000]}"
    finally:
        from tools.computer_use.tool import release_computer_use_session
        release_computer_use_session("slim-smoke")
    print(json.dumps({"ok": True, "display": os.environ.get("DISPLAY"), **summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
