"""Live: computer_use through Hermes' tool layer against a real cua-driver on a private Xvfb display.

Opt-in (needs a driver binary plus Xvfb and xterm on PATH), so the hermetic suite skips it:

    HERMES_TEST_CUA_DRIVER=~/.hermes/tools/cua-driver-0.21.0-linux-x64/cua-driver \\
        scripts/run_tests.sh tests/computer_use/test_cua_xvfb_live.py

Proves, with the default configuration (full permissions, no approval callback, nobody to ask): the
driver starts in its ``unrestricted`` mode, ``list_windows`` reports the xterm the test mapped, and
``capture`` returns a screenshot of it. CI runs the same flow against the bundled driver in
``slim/smoke_test.py`` (slim-bundle workflow, linux-x64 leg).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.waiting import wait_until

pytestmark = pytest.mark.platforms("linux")

_BINARY = os.path.expanduser(os.environ.get("HERMES_TEST_CUA_DRIVER", ""))
_TITLE = "hermes-cua-live"


@pytest.fixture
def x_display(monkeypatch):
    xvfb, xterm = shutil.which("Xvfb"), shutil.which("xterm")
    if not (_BINARY and os.access(_BINARY, os.X_OK) and xvfb and xterm):
        pytest.skip("set HERMES_TEST_CUA_DRIVER and install Xvfb + xterm to run the live computer_use test")
    read_fd, write_fd = os.pipe()
    server = subprocess.Popen([xvfb, "-displayfd", str(write_fd), "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
                              pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(write_fd)
    with os.fdopen(read_fd) as ready:  # Xvfb writes the display number once it accepts connections
        display = ":" + ready.readline().strip()
    env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY", "XAUTHORITY")}
    client = subprocess.Popen([xterm, "-T", _TITLE, "-e", "sleep", "300"], env=dict(env, DISPLAY=display),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for var in ("WAYLAND_DISPLAY", "XAUTHORITY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISPLAY", display)
    monkeypatch.setenv("HERMES_CUA_DRIVER_CMD", _BINARY)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    yield display
    from tools.computer_use.tool import release_computer_use_session
    release_computer_use_session("live")
    for proc in (client, server):
        proc.terminate()
        proc.wait(timeout=30)


def _call(action: str, **args):
    from tools.computer_use.tool import handle_computer_use

    out = handle_computer_use({"action": action, **args}, session_id="live")
    return json.loads(out) if isinstance(out, str) else out


def test_list_windows_and_screenshot_with_full_permissions(x_display):
    from tools.computer_use import tool

    listing = wait_until(lambda: (w := _call("list_windows")) and _TITLE in json.dumps(w) and w,
                         timeout=30, what=f"{_TITLE} in list_windows")
    assert tool._backend_permission_modes["live"] == "unrestricted"
    app = next(w.get("app_name") for w in listing["windows"] if _TITLE in str(w.get("title")))
    capture = _call("capture", mode="vision", app=app)
    if capture.get("_multimodal"):
        assert any(p.get("type") == "image_url" for p in capture["content"])
    else:
        assert capture.get("screenshot_path") and Path(capture["screenshot_path"]).stat().st_size > 0, capture
