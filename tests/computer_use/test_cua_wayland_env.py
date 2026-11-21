from unittest.mock import patch

import pytest

from tools.computer_use import cua_backend

pytestmark = pytest.mark.platforms("linux")

_VAR = "CUA_DRIVER_RS_ENABLE_WAYLAND"


def _child_env(base_env, native_wayland=None):
    config = {"computer_use": {} if native_wayland is None else {"native_wayland": native_wayland}}
    with patch("hermes_cli.config.load_config", return_value=config):
        return cua_backend.cua_driver_child_env(base_env)


def test_configured_native_wayland_reaches_linux_wayland_child():
    assert _child_env({"WAYLAND_DISPLAY": "wayland-1"}, True)[_VAR] == "1"


def test_native_wayland_not_injected_without_wayland_display_or_opt_in():
    assert _VAR not in _child_env({"DISPLAY": ":0"}, True)
    assert _VAR not in _child_env({"WAYLAND_DISPLAY": "wayland-1"}, False)


def test_auto_enables_native_wayland_on_wlroots_only():
    """Default ``auto``: Hyprland/Sway get the native backend; KDE/GNOME stay on XWayland, where it has no adapter."""
    assert _child_env({"WAYLAND_DISPLAY": "wayland-1", "HYPRLAND_INSTANCE_SIGNATURE": "x"})[_VAR] == "1"
    assert _child_env({"WAYLAND_DISPLAY": "wayland-1", "XDG_CURRENT_DESKTOP": "sway"}, "auto")[_VAR] == "1"
    assert _VAR not in _child_env({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "KDE"})


def test_stale_xauthority_is_replaced_in_the_driver_env(tmp_path):
    (tmp_path / "xauth_live").write_text("cookie")
    env = _child_env({"DISPLAY": ":0", "XAUTHORITY": str(tmp_path / "xauth_gone"), "XDG_RUNTIME_DIR": str(tmp_path)})
    assert env["XAUTHORITY"] == str(tmp_path / "xauth_live")
