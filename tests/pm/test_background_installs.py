"""The background installer's contracts: derived pending set, retries, contention, repair."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import pytest

from pm import background
from pm.package import InstallError


@pytest.fixture()
def world(monkeypatch, tmp_path):
    """Real locks and state in a temp partials root; only "is it installed" and the fetch are stubbed."""
    from pm import paths

    monkeypatch.setattr(paths, "partials_root", lambda: tmp_path / "partials")
    facts = tmp_path / "facts.json"
    facts.write_text("{}")
    monkeypatch.setattr(paths, "facts_path", lambda: facts)
    installed: set[str] = set()
    monkeypatch.setattr(background, "_installed", lambda name: name in installed)
    monkeypatch.setattr(background, "wanted", lambda: ["node", "npm", "ffmpeg"])
    monkeypatch.setattr(background, "allowed", lambda: True)
    monkeypatch.setattr(background, "_version", lambda name: "1")
    monkeypatch.setattr(background, "_prefetch", lambda name, stop: None)
    monkeypatch.setattr(background, "_spawn", lambda: None)
    return installed


def _ensure_stub(monkeypatch, fn):
    import pm.install

    monkeypatch.setattr(pm.install, "ensure", fn)


def test_real_registry_defers_the_large_tools_only():
    from pm.registry import get_package, source_install_packages

    assert all(get_package(name).background for name in ("node", "npm", "ffmpeg", "chromium", "cua-driver"))
    names = ["uv", "ripgrep", "node", "npm", "ffmpeg", "chromium"]
    kept = source_install_packages(names, include_background=False)
    assert not {"node", "npm", "ffmpeg", "chromium"} & set(kept)
    assert "ripgrep" in kept
    assert "ffmpeg" in source_install_packages(names)  # Shells, CI and images stay foreground.


def test_daemon_installs_all_pending_then_exits_and_nothing_remains(world, monkeypatch):
    _ensure_stub(monkeypatch, lambda name, **kw: world.add(name))
    assert background.run() == 0
    assert world == {"node", "npm", "ffmpeg"}
    assert background.pending() == []


def test_interrupted_daemon_resumes_from_what_is_derived(world, monkeypatch):
    world.add("node")  # A prior daemon finished node, then died with no goodbye.
    (background._directory() / "state.json").write_text("{ torn write")
    seen = []
    _ensure_stub(monkeypatch, lambda name, **kw: (seen.append(name), world.add(name)))
    background.run()
    assert seen == ["npm", "ffmpeg"]


def test_failure_backs_off_without_blocking_the_next_package_then_stops_retrying(world, monkeypatch):
    def flaky(name, **kw):
        if name == "npm":
            raise InstallError("npm", "offline")
        world.add(name)

    _ensure_stub(monkeypatch, flaky)
    background._install_one("npm", False, threading.Event())
    state = background._load()
    assert not background._due(state, "npm", time.time())
    assert background._due(state, "npm", time.time() + background.BASE_BACKOFF_SECONDS + 1)

    background._update(lambda s: s["packages"]["npm"].update(attempts=background.MAX_ATTEMPTS, next_attempt=0))
    background.run()  # Must not sleep forever on an exhausted package.
    assert world == {"node", "ffmpeg"}
    assert background.status()["packages"]["npm"]["status"] == "failed"


def test_failure_against_an_old_pin_does_not_block_a_new_pin(world, monkeypatch):
    background._update(lambda s: s.setdefault("packages", {}).update(
        npm={"version": "0", "attempts": background.MAX_ATTEMPTS, "next_attempt": 0}))
    assert background._due(background._load(), "npm", time.time())


def test_repair_revives_failed_package_and_verifies_installed_bytes(world, monkeypatch):
    world.update({"node", "ffmpeg"})
    background._update(lambda s: s.setdefault("packages", {}).update(
        npm={"version": "1", "attempts": background.MAX_ATTEMPTS, "next_attempt": 0, "error": "x"}))
    assert background.repair(["npm", "node"]) == ["npm", "node"]
    calls = {}
    _ensure_stub(monkeypatch, lambda name, **kw: (calls.__setitem__(name, kw["verify"]), world.add(name)))
    background.run()
    assert calls == {"node": True, "npm": True}  # Installed node is still re-verified.
    assert background._load()["repair"] == []


def test_repair_rejects_a_package_the_daemon_does_not_own(world):
    with pytest.raises(InstallError):
        background.repair(["ripgrep"])


def test_stop_stays_stopped_until_start(world, monkeypatch):
    spawned = []
    monkeypatch.setattr(background, "_spawn", lambda: spawned.append(1))
    background._update(lambda s: s.update(paused=True))
    assert background.kick() is False
    background._update(lambda s: s.pop("paused"))
    assert background.kick() is True and spawned


def test_foreground_waits_for_the_daemons_download(world):
    released = threading.Event()
    done = []
    with background._package_lock("ffmpeg"):
        thread = threading.Thread(target=lambda: (background.wait_for_download("ffmpeg"), done.append(released.is_set())))
        thread.start()
        thread.join(0.5)  # Still blocked after this: it is waiting on the lock.
        assert not done
        released.set()
    thread.join(5)
    assert done == [True]


def test_only_one_daemon_and_a_killed_holder_frees_the_lock(world, monkeypatch):
    lock = background._directory() / "driver.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys,time\nfrom pm.filesystem import lock_path\nfrom pathlib import Path\n"
         f"lock_path(Path({str(lock)!r}), timeout=None)\nprint('held', flush=True)\ntime.sleep(60)"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        ran = []
        _ensure_stub(monkeypatch, lambda name, **kw: ran.append(name))
        assert background.run() == 0 and not ran
        assert background.kick() is False
    finally:
        holder.kill()
        holder.wait()
    assert not background.daemon_running()


def test_status_is_json_serialisable(world):
    json.dumps(background.status())
