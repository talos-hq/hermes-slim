"""Computer use is on by default with full permissions, and repairs a missing driver on first use.

Contracts:
* A fresh profile enables the ``computer_use`` toolset, auto-approves its actions and runs cua-driver in its
  ``unrestricted`` mode; ``computer_use.auto_approve: false`` restores prompts and the ``standard`` mode, and a
  reviewed ``bounded`` manifest is never widened.
* The toolset is admitted while the pinned driver is missing or off-pin, as long as PM may install it, so the
  first call (``CuaDriverBackend.start``) provisions it; a declined package, disabled lazy installs or an
  explicit override keep it out. A driver baked in beside the install tree (hermes-slim) is used without PM.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.computer_use import cua_backend, cua_backend_driver


def _set_computer_use(**values):
    from hermes_cli.config import load_config, save_config

    cfg = load_config()
    cfg.setdefault("computer_use", {}).update(values)
    save_config(cfg)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    from tools.computer_use.tool import reset_backend_for_tests

    monkeypatch.delenv("HERMES_CUA_DRIVER_CMD", raising=False)
    reset_backend_for_tests()
    yield
    reset_backend_for_tests()


# ── defaults ────────────────────────────────────────────────────────────────

def test_fresh_profile_enables_the_toolset_and_auto_approves():
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools

    config = load_config()
    assert "computer_use" in _get_platform_tools(config, "cli", include_default_mcp_servers=False)
    assert config["computer_use"]["auto_approve"] is True
    assert cua_backend.cua_auto_approve() is True


def test_default_session_runs_the_driver_unrestricted_without_a_bypass():
    from tools.computer_use import tool

    with patch("tools.approval.is_approval_bypass_active_for_session", return_value=False):
        assert tool._cua_permission_mode("session-a") == "unrestricted"


def test_opt_out_restores_standard_mode():
    from tools.computer_use import tool

    _set_computer_use(auto_approve=False)
    with patch("tools.approval.is_approval_bypass_active_for_session", return_value=False):
        assert tool._cua_permission_mode("session-a") == "standard"


def test_bounded_manifest_is_never_widened_by_auto_approve():
    from tools.computer_use import tool

    _set_computer_use(permission_mode="bounded")
    with patch("tools.approval.is_approval_bypass_active_for_session", return_value=False):
        assert tool._cua_permission_mode("session-a") == "bounded"


def test_unreadable_config_keeps_prompting():
    with patch("hermes_cli.config.load_config", side_effect=OSError("corrupt")):
        assert cua_backend.cua_auto_approve() is False


class _RecordingBackend:
    def __init__(self):
        self.calls = []

    def start(self):
        pass

    def stop(self):
        pass

    def is_available(self):
        return True

    def click(self, **kw):
        from tools.computer_use.backend import ActionResult

        self.calls.append(("click", kw))
        return ActionResult(ok=True, action="click")


def _click_without_anyone_to_ask(monkeypatch):
    """A destructive action in a headless context (no CLI callback, no gateway): the case that used to refuse."""
    from tools.computer_use import tool

    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    backend = _RecordingBackend()
    tool._backend = backend
    out = json.loads(tool.handle_computer_use({"action": "click", "coordinate": [10, 10]}))
    return out, backend


def test_default_never_prompts_for_desktop_actions(monkeypatch):
    with patch("tools.approval._run_approval_gate", side_effect=AssertionError("prompted")):
        out, backend = _click_without_anyone_to_ask(monkeypatch)
    assert "error" not in out, out
    assert backend.calls and backend.calls[0][0] == "click"


def test_opt_out_goes_through_the_approval_gate(monkeypatch):
    _set_computer_use(auto_approve=False)
    out, backend = _click_without_anyone_to_ask(monkeypatch)
    assert "BLOCKED" in out.get("error", ""), out
    assert backend.calls == []


def test_hard_blocks_still_apply_with_full_permissions():
    from tools.computer_use import tool

    out = json.loads(tool.handle_computer_use({"action": "key", "keys": "ctrl+alt+delete"}))
    assert "blocked key combo" in out.get("error", ""), out


# ── provisioning ────────────────────────────────────────────────────────────

class _Installed:
    binary = Path("/opt/pm/cua-driver")


@pytest.mark.parametrize(("installed", "declined", "lazy", "admitted"), [
    (_Installed(), frozenset(), True, True),                 # installed at the pin
    (None, frozenset(), True, True),                          # missing or off-pin: first use installs it
    (None, frozenset({"cua-driver"}), True, False),           # the user declined the package
    (None, frozenset(), False, False),                        # security.allow_lazy_installs: false
])
def test_toolset_admission_follows_provisioning(installed, declined, lazy, admitted):
    from plugins.computer_use.cua import CuaDriverProvider

    with patch("pm.installed_package", return_value=installed), \
            patch.object(cua_backend_driver, "bundled_cua_driver_cmd", return_value=None), \
            patch("pm.defaults.declined", return_value=declined), \
            patch("pm.install.lazy_installs_allowed", return_value=lazy), \
            patch("tools.bot_desktop.placement.resolve") as placement:
        placement.return_value.where = "gateway"
        assert CuaDriverProvider().is_available() is admitted


def test_explicit_override_is_never_provisioned(monkeypatch):
    monkeypatch.setenv("HERMES_CUA_DRIVER_CMD", "/nowhere/cua-driver")
    assert cua_backend_driver.cua_driver_provisionable() is False


def _start_backend_recording_ensure(contract):
    backend = cua_backend.CuaDriverBackend()
    ensured = []
    with patch("pm.ensure", side_effect=lambda name, **kw: ensured.append(name)), \
            patch.object(cua_backend, "sandbox_mcp_invocation", return_value=None), \
            patch.object(cua_backend, "cua_driver_runtime_contract_status", return_value=contract), \
            pytest.raises(RuntimeError, match="not ready"):
        backend.start()
    return ensured


def test_first_use_installs_a_missing_or_off_pin_driver():
    with patch.object(cua_backend_driver, "bundled_cua_driver_cmd", return_value=None):
        ensured = _start_backend_recording_ensure({"ready": False, "reason": "stop after ensure"})
    assert ensured == ["cua-driver"]


def test_bundled_driver_is_used_without_pm(tmp_path):
    app = tmp_path / "hermes-slim" / "app"
    app.mkdir(parents=True)
    rel = {"darwin": "CuaDriver.app/Contents/MacOS/cua-driver", "win32": "cua-driver.exe"}.get(sys.platform, "cua-driver")
    driver = tmp_path / "hermes-slim" / "tools" / "cua-driver" / rel
    driver.parent.mkdir(parents=True)
    driver.write_bytes(b"")
    driver.chmod(driver.stat().st_mode | stat.S_IXUSR)
    with patch("pm.paths.repo_root", return_value=app), \
            patch("pm.installed_package", side_effect=AssertionError("PM consulted")):
        assert cua_backend_driver.resolve_cua_driver_cmd() == str(driver)
        assert cua_backend_driver.external_cua_driver_cmd() == str(driver)
        assert _start_backend_recording_ensure({"ready": False, "reason": "stop"}) == []
