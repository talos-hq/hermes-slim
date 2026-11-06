"""The background installer's contracts: derived pending set, retries, contention, repair."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

import pytest

from pm import background
from tests.waiting import wait_until
from pm.package import InstallError

_real_prefetch = background._prefetch


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
    monkeypatch.setattr(background, "_prefetch", lambda name, stop, repair=False: None)
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


def test_repair_refetches_damaged_bytes_before_taking_the_store_lock(world, monkeypatch, tmp_path):
    """Damaged but present: facts say installed, so only a verifying check sends it to the prefetch."""
    import pm.install
    import pm.registry
    from pm import paths
    from pm.store import Store

    package = type("P", (), {"name": "ffmpeg"})()
    monkeypatch.setattr(pm.registry, "walk", lambda names: [package])
    monkeypatch.setattr(pm.install, "_installed_location",
                        lambda p, l, t, verify=False: None if verify else object())
    monkeypatch.setattr(pm.install, "_lockfile", lambda: type("L", (), {"artifacts": lambda self, n, t: [{"sha256": "x"}]})())
    monkeypatch.setattr(paths, "writable_store_root", lambda: tmp_path / "store")
    scratches = []
    monkeypatch.setattr(Store, "fetch_many", lambda self, a, scratch, **kw: scratches.append(scratch))
    _real_prefetch("ffmpeg", threading.Event())
    assert not scratches  # An ordinary pass trusts facts.
    _real_prefetch("ffmpeg", threading.Event(), True)
    assert len(scratches) == 1
    assert not scratches[0].name.startswith(".staging-")  # `pm gc` deletes those as abandoned.
    assert not scratches[0].exists()


def test_killed_daemons_scratch_is_reclaimed_on_next_run(world, monkeypatch, tmp_path):
    from pm import paths

    monkeypatch.setattr(paths, "writable_store_root", lambda: tmp_path / "store")
    leftover = tmp_path / "store" / ".prefetch-dead"
    leftover.mkdir(parents=True)
    _ensure_stub(monkeypatch, lambda name, **kw: world.add(name))
    background.run()
    assert not leftover.exists()


def test_update_defers_large_tools_when_the_worker_allows_and_keeps_them_when_it_cannot_say(monkeypatch):
    import pm.client
    import pm.install
    import pm.libatomic

    ensured = []
    monkeypatch.setattr(pm.client, "is_runtime", lambda: False)
    monkeypatch.setattr(pm.client, "ensure", lambda name, **kw: ensured.append(name))
    monkeypatch.setattr(pm.libatomic, "install_before_lock", lambda: None)
    monkeypatch.setattr(pm.install, "activate", lambda **kw: [])
    monkeypatch.setattr(pm.client, "_request", lambda op, args: True)
    pm.client.ensure_tools_for_sync()
    assert ensured and "ffmpeg" not in ensured and "node" not in ensured

    ensured.clear()

    def unavailable(op, args):
        raise InstallError("pm-runtime", "no runtime")

    monkeypatch.setattr(pm.client, "_request", unavailable)
    pm.client.ensure_tools_for_sync()
    assert "ffmpeg" in ensured or "node" in ensured


def test_unsupported_architecture_never_breaks_startup(world, monkeypatch):
    def unsupported(state=None):
        raise RuntimeError("unsupported architecture")

    monkeypatch.setattr(background, "pending", unsupported)
    assert background.kick() is False
    background.watch_from_cli()


def test_repair_during_a_long_backoff_starts_promptly(world, monkeypatch):
    world.update({"node", "ffmpeg"})
    background._update(lambda s: s.setdefault("packages", {}).update(
        npm={"version": "1", "attempts": 3, "next_attempt": time.time() + 3600}))
    _ensure_stub(monkeypatch, lambda name, **kw: world.add(name))
    daemon = threading.Thread(target=background.run, daemon=True)
    daemon.start()
    wait_until(lambda: background._load().get("pid"), timeout=10)
    background.repair(["npm"])
    daemon.join(15)
    assert not daemon.is_alive() and "npm" in world


def test_deferred_install_still_records_which_checkout_owns_the_state(monkeypatch):
    import argparse
    import pm.background
    import pm.cli
    import pm.environments
    import pm.install

    recorded = []
    monkeypatch.setattr(pm.cli, "_install_names", lambda names, target=None, *, verify=True: 0)
    monkeypatch.setattr(pm.install, "sync_venv", lambda *a, **k: None)
    monkeypatch.setattr(pm.install, "activate", lambda **k: [])
    monkeypatch.setattr(pm.background, "allowed", lambda: True)
    monkeypatch.setattr(pm.background, "kick", lambda: True)
    monkeypatch.setattr(pm.environments, "record_activation_inputs", lambda *a, **k: recorded.append(a))
    assert pm.cli.cmd_install(argparse.Namespace(names=None, extra=[], target=None, tools_only=False,
                                                 defer_large=True)) == 0
    assert recorded


@pytest.mark.platforms("windows")
def test_windows_daemon_breaks_away_from_the_update_job_or_falls_back(world, monkeypatch):
    monkeypatch.undo()
    flags = []

    def popen(argv, **kwargs):
        flags.append(kwargs["creationflags"])
        if len(flags) == 1:
            raise PermissionError("job forbids breakaway")

    monkeypatch.setattr(subprocess, "Popen", popen)
    background._spawn()
    assert flags[0] & subprocess.CREATE_BREAKAWAY_FROM_JOB and not flags[1] & subprocess.CREATE_BREAKAWAY_FROM_JOB
