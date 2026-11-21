"""Linux display-session classification for computer use (``tools.computer_use.linux_display``).

Pure functions of an environment mapping: what cua-driver can see on X11, on wlroots Wayland (native backend),
on GNOME / KDE Wayland (XWayland only) and with no display, plus the stale-XAUTHORITY repair. Measured on a
KDE Plasma 6 Wayland session (cua-driver 0.21): the X11 path listed no native windows and the native backend
failed with "compositor does not expose zwlr_foreign_toplevel_manager_v1".
"""

from __future__ import annotations

import pytest

from tools.computer_use import linux_display as ld


@pytest.mark.parametrize(("env", "kind", "desktop", "native"), [
    ({"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11", "XDG_CURRENT_DESKTOP": "XFCE"}, "x11", "other", False),
    ({"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":1", "HYPRLAND_INSTANCE_SIGNATURE": "abc"}, "wayland", "hyprland", True),
    ({"WAYLAND_DISPLAY": "wayland-1", "XDG_CURRENT_DESKTOP": "sway"}, "wayland", "sway", True),
    ({"WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "labwc:wlroots"}, "wayland", "labwc", True),
    ({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "KDE"}, "wayland", "kde", False),
    ({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, "wayland", "gnome", False),
    ({}, "headless", "", False),
])
def test_session_classification(env, kind, desktop, native):
    session = ld.detect(env)
    assert (session.kind, session.desktop, session.native_wayland_capable) == (kind, desktop, native)


@pytest.mark.parametrize(("setting", "env", "expected"), [
    ("auto", {"WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "Hyprland"}, True),
    ("auto", {"WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "KDE"}, False),
    (True, {"WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "KDE"}, True),      # explicit opt-in wins
    ("false", {"WAYLAND_DISPLAY": "w", "XDG_CURRENT_DESKTOP": "sway"}, False),  # `hermes config set` spelling
    (True, {"DISPLAY": ":0"}, False),                                           # never on X11
])
def test_native_wayland_setting(setting, env, expected):
    assert ld.resolve_native_wayland(setting, ld.detect(env)) is expected


def test_guidance_names_the_fallbacks_where_only_xwayland_is_visible():
    text = ld.guidance(ld.detect({"WAYLAND_DISPLAY": "w", "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "KDE"}))
    assert "only XWayland apps" in text
    for fallback in ("QT_QPA_PLATFORM=xcb", "browser_exec", "hermes computer-use screen start"):
        assert fallback in text


def test_stale_xauthority_is_detected_and_repaired_from_the_single_live_cookie(tmp_path):
    live = tmp_path / "xauth_live"
    live.write_text("cookie")
    env = {"DISPLAY": ":0", "XAUTHORITY": str(tmp_path / "xauth_gone"), "XDG_RUNTIME_DIR": str(tmp_path)}
    assert ld.detect(env).stale_xauthority
    assert "XAUTHORITY" in ld.guidance(ld.detect(env))
    assert ld.live_xauthority(env) == str(live)


def test_ambiguous_or_valid_xauthority_is_left_alone(tmp_path):
    for name in ("xauth_a", "xauth_b"):
        (tmp_path / name).write_text("cookie")
    stale = {"DISPLAY": ":0", "XAUTHORITY": str(tmp_path / "gone"), "XDG_RUNTIME_DIR": str(tmp_path)}
    assert ld.live_xauthority(stale) is None
    valid = dict(stale, XAUTHORITY=str(tmp_path / "xauth_a"))
    assert not ld.detect(valid).stale_xauthority and ld.live_xauthority(valid) is None
