"""macOS TCC grants cannot be given silently: Hermes asks once per machine and never again.

``request_tcc_grant_once`` takes the platform as data, so the decision is exercised here on any host; the
spawn is recorded rather than run (the real ``cua-driver permissions grant`` raises a macOS dialog).
"""

from __future__ import annotations

from unittest.mock import patch

from tools.computer_use import permissions


def test_requests_once_then_never_again():
    with patch.object(permissions.subprocess, "Popen") as popen:
        assert permissions.request_tcc_grant_once("/opt/CuaDriver.app/Contents/MacOS/cua-driver",
                                                  platform="darwin") == "requested"
        assert permissions.request_tcc_grant_once("/opt/CuaDriver.app/Contents/MacOS/cua-driver",
                                                  platform="darwin") == "already_requested"
    assert popen.call_count == 1
    argv = popen.call_args.args[0]
    assert argv[1:] == ["permissions", "grant"]
    assert popen.call_args.kwargs["start_new_session"] is True  # never blocks the tool call or setup
    assert permissions.tcc_grant_marker().is_file()


def test_no_request_off_macos_or_without_a_driver():
    with patch.object(permissions.subprocess, "Popen") as popen:
        assert permissions.request_tcc_grant_once("/usr/bin/cua-driver", platform="linux") == "not_macos"
        assert permissions.request_tcc_grant_once(None, platform="darwin") == "missing_driver"
    popen.assert_not_called()
    assert not permissions.tcc_grant_marker().exists()  # a later run with a driver still asks
