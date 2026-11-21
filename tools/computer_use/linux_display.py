"""Which Linux display session cua-driver would see, and what that means for computer use.

cua-driver 0.21 drives X11 (and XWayland) through XTest/XSendEvent + AT-SPI. Its native Wayland backend
(``CUA_DRIVER_RS_ENABLE_WAYLAND=1``) needs the wlroots protocols (foreign-toplevel, screencopy,
virtual-pointer/keyboard) that Hyprland, Sway, labwc, river and Wayfire expose; GNOME needs cua's
``winrects@cua`` Shell extension; KDE Plasma (KWin) has no adapter. On GNOME / KDE Wayland the driver
therefore only sees XWayland clients, so a desktop of native Wayland apps reads as "no windows".

Everything here is a pure function of an environment mapping (plus ``os.path.exists`` for the X
authority file), so callers pass the child env they will actually spawn the driver with.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from typing import Mapping, Optional

# XDG_CURRENT_DESKTOP / XDG_SESSION_DESKTOP tokens (lower-case) for compositors whose wlroots-style
# protocols the native backend uses (cua-driver's own error text names sway, labwc and hyprland).
_WLROOTS = ("hyprland", "sway", "labwc", "river", "wayfire")
_ENV_MARKERS = (("HYPRLAND_INSTANCE_SIGNATURE", "hyprland"), ("SWAYSOCK", "sway"))


@dataclass(frozen=True)
class LinuxDisplaySession:
    kind: str                 # "x11" | "wayland" | "headless"
    desktop: str              # "hyprland" | "sway" | ... | "gnome" | "kde" | "other" | ""
    xwayland: bool            # an X11 DISPLAY is reachable beside Wayland
    native_wayland_capable: bool  # cua-driver's native Wayland backend works on this compositor
    stale_xauthority: bool    # XAUTHORITY names a file that no longer exists (X11 connections are refused)

    @property
    def wlroots(self) -> bool:
        return self.desktop in _WLROOTS


def _desktop(env: Mapping[str, str]) -> str:
    for var, name in _ENV_MARKERS:
        if env.get(var):
            return name
    tokens = ":".join(env.get(v, "") for v in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"))
    tokens = tokens.lower()
    for name in _WLROOTS:
        if name in tokens:
            return name
    if "gnome" in tokens or "ubuntu" in tokens:
        return "gnome"
    if "kde" in tokens or "plasma" in tokens:
        return "kde"
    return "other" if tokens.strip(":") else ""


def detect(env: Optional[Mapping[str, str]] = None) -> LinuxDisplaySession:
    """Classify the session described by ``env`` (default ``os.environ``)."""
    env = os.environ if env is None else env
    wayland = bool(env.get("WAYLAND_DISPLAY")) or env.get("XDG_SESSION_TYPE") == "wayland"
    display = bool(env.get("DISPLAY"))
    kind = "wayland" if wayland else "x11" if display else "headless"
    desktop = _desktop(env) if kind != "headless" else ""
    xauth = env.get("XAUTHORITY", "")
    return LinuxDisplaySession(
        kind=kind, desktop=desktop, xwayland=wayland and display,
        native_wayland_capable=wayland and desktop in _WLROOTS,
        stale_xauthority=display and bool(xauth) and not os.path.exists(xauth))


def resolve_native_wayland(setting: object, session: LinuxDisplaySession) -> bool:
    """``computer_use.native_wayland``: true/false force it (bool or the usual spellings); anything else is
    ``auto`` — on exactly where the native backend works (a wlroots compositor)."""
    if session.kind != "wayland":
        return False
    text = str(setting).strip().lower()
    if setting is True or text in {"true", "on", "yes", "1"}:
        return True
    if setting is False or text in {"false", "off", "no", "0"}:
        return False
    return session.native_wayland_capable


def live_xauthority(env: Mapping[str, str]) -> Optional[str]:
    """The X authority file to use when ``XAUTHORITY`` is stale: the single ``xauth_*`` cookie in
    ``XDG_RUNTIME_DIR`` (where KWin, Mutter and Hyprland's XWayland write theirs). None when it is not
    stale or the replacement is ambiguous — guessing between cookies is worse than the clear error."""
    runtime = env.get("XDG_RUNTIME_DIR", "")
    if not runtime or not detect(env).stale_xauthority:
        return None
    found = glob.glob(os.path.join(glob.escape(runtime), "xauth_*"))
    return found[0] if len(found) == 1 else None


def guidance(session: LinuxDisplaySession) -> str:
    """One paragraph telling the agent / user what this session supports and the fallback when it does not."""
    if session.kind == "headless":
        return ("No display: set DISPLAY, or start Hermes' own X11 screen with `hermes computer-use screen start` "
                "(Bot Screen). For web pages use the browser tools (browser_exec / browser_cdp) instead.")
    stale = (" XAUTHORITY points at a file that no longer exists, so X11 connections are refused: restart the "
             "Hermes process from the current desktop session (or export the session's current XAUTHORITY)."
             if session.stale_xauthority else "")
    if session.kind == "x11":
        return "X11 session: fully supported (windows, screenshots, background input)." + stale
    if session.native_wayland_capable:
        return (f"Wayland on {session.desktop} (wlroots): Hermes enables cua-driver's native Wayland backend, so "
                "native Wayland windows are listed, captured and driven; XWayland apps work too." + stale)
    who = {"gnome": "GNOME", "kde": "KDE Plasma"}.get(session.desktop, "this compositor")
    helper = (" On GNOME, installing cua's Shell helper (`<cua-driver dir>/wayland-helper/install.sh`, then log out "
              "and in) plus `computer_use.native_wayland: true` adds native windows." if session.desktop == "gnome"
              else "")
    return (f"Wayland on {who}: cua-driver has no native backend here, so it sees only XWayland apps; native "
            "Wayland windows are invisible and full-screen capture is unavailable." + helper + " Workarounds: run "
            "the target app under XWayland (Qt: QT_QPA_PLATFORM=xcb, GTK: GDK_BACKEND=x11, Chromium/Electron: "
            "--ozone-platform=x11), use the browser tools (browser_exec / browser_cdp) for web pages, or drive "
            "Hermes' own X11 screen (`hermes computer-use screen start`)." + stale)
