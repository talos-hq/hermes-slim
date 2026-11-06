"""Large tools (``Package.background``) install in a detached daemon, not in the installer.

What is pending is DERIVED: wanted packages (lockfile, target, opt-outs) minus what facts say is
installed. So every interruption resumes by itself and a lost state file costs nothing; state only
carries retry bookkeeping, progress and repair requests.

Locks, all machine-scoped beside the partials they guard, all released by the OS when a holder dies:

- ``driver.lock``: at most one daemon per machine. Spawning is therefore idempotent.
- ``package-<name>.lock``: held while the daemon downloads, WITHOUT the store lock, so a 170 MB
  fetch never blocks other PM operations. A foreground ``ensure`` waits on it (``wait_for_download``)
  and then finds the archive in the fetch cache instead of downloading it a second time. The daemon
  releases it before taking the store lock, so the two locks are never held in the opposite order
  of a foreground caller that already has the store lock.
- ``state.lock``: read-modify-write of ``state.json``.

Byte-level resume is the downloader's: ``.part``/``.ranges`` survive a killed daemon.

Stdlib-only apart from PM's own modules: the installers run this before application deps exist.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from pm.filesystem import lock_fd, lock_path

MAX_ATTEMPTS = 8
BASE_BACKOFF_SECONDS = 60
MAX_BACKOFF_SECONDS = 60 * 60
_PROGRESS_INTERVAL_SECONDS = 2
_CONTROL_POLL_SECONDS = 2
_WAIT_NOTICE_SECONDS = 2
_LOG_LIMIT_BYTES = 1024 * 1024


def _directory() -> Path:
    from pm.paths import partials_root

    path = partials_root() / ".background"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── State ────────────────────────────────────────────────────────────────────────────────────────

@contextmanager
def _state_lock():
    fd = lock_path(_directory() / "state.lock", timeout=None)
    try:
        yield
    finally:
        os.close(fd)


def _load() -> dict:
    path = _directory() / "state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # Bookkeeping only; the pending set is derived, so starting over loses nothing but retry counts.
        return {}
    return state if isinstance(state, dict) else {}


def _save(state: dict) -> None:
    from pm.filesystem import durable_write_bytes

    durable_write_bytes(_directory() / "state.json", (json.dumps(state, indent=2) + "\n").encode("utf-8"))


def _update(mutate) -> dict:
    with _state_lock():
        state = _load()
        mutate(state)
        _save(state)
    return state


def _record(name: str, **fields) -> None:
    _update(lambda state: state.setdefault("packages", {}).setdefault(name, {}).update(fields))


# ── What is pending ──────────────────────────────────────────────────────────────────────────────

def allowed() -> bool:
    """May large tools be deferred on this install? Sealed payloads ship them, and the lazy-install
    policy (config or the Docker/test bridge) means the user asked Hermes not to fetch on its own."""
    from pm.install import lazy_installs_allowed, sealed

    return not sealed() and lazy_installs_allowed()


def _version(name: str) -> str | None:
    from pm.install import _lockfile

    return _lockfile().version(name)


def wanted() -> list[str]:
    """Deferred packages this install should carry, dependencies first."""
    from pm.defaults import declined
    from pm.install import _lockfile
    from pm.registry import get_package, walk
    from pm.store import current_target

    target, refused, names = current_target(), declined(), []
    for name in _lockfile().names():
        try:
            package = get_package(name)
        except KeyError:
            continue  # Lockfile/definition skew mid-update.
        if (package.background and not package.internal and name not in refused
                and package.missing_reason(target) is None):
            names.append(name)
    ordered = [package.name for package in walk(names) if package.background]
    return list(dict.fromkeys(ordered))


def _installed(name: str) -> bool:
    from pm.install import _installed_location, _lockfile
    from pm.registry import get_package
    from pm.store import current_target

    return _installed_location(get_package(name), _lockfile(), current_target()) is not None


def pending(state: dict | None = None) -> list[str]:
    from pm import paths

    # An install PM never touched is not PM's to complete (dev venvs, foreign trees).
    if not paths.facts_path().is_file() and not paths.runtime_facts_path().is_file():
        return []
    state = _load() if state is None else state
    repairs = state.get("repair") or []
    return [name for name in wanted() if name in repairs or not _installed(name)]


def _entry(state: dict, name: str) -> dict:
    """A failure recorded against an older pin says nothing about the current one."""
    entry = (state.get("packages") or {}).get(name) or {}
    return entry if entry.get("version") == _version(name) else {}


def _due(state: dict, name: str, now: float) -> bool:
    entry = _entry(state, name)
    return entry.get("attempts", 0) < MAX_ATTEMPTS and entry.get("next_attempt", 0) <= now


def _backoff(attempts: int) -> float:
    return min(BASE_BACKOFF_SECONDS * 2 ** (attempts - 1), MAX_BACKOFF_SECONDS)


# ── Locks ────────────────────────────────────────────────────────────────────────────────────────

@contextmanager
def _package_lock(name: str, *, wait: bool = True):
    fd = lock_path(_directory() / f"package-{name}.lock", timeout=None if wait else 0)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)


def daemon_running() -> bool:
    fd = lock_path(_directory() / "driver.lock", timeout=0)
    if fd is None:
        return True
    os.close(fd)
    return False


def wait_for_download(name: str, *, cancelled=None) -> None:
    """Foreground barrier: let the daemon's download of ``name`` finish rather than repeat it."""
    with (_directory() / f"package-{name}.lock").open("a+b") as handle:
        started = time.monotonic()
        noticed = False
        while not lock_fd(handle.fileno(), wait=False):
            if cancelled is not None and cancelled():
                return
            if not noticed and time.monotonic() - started >= _WAIT_NOTICE_SECONDS:
                print(f"waiting for the background install of {name}", file=sys.stderr, flush=True)
                noticed = True
            time.sleep(0.2)


# ── One package ──────────────────────────────────────────────────────────────────────────────────

def _reclaim_prefetch_scratch() -> None:
    """A killed daemon's leftovers. Caller holds the driver lock, so none of these is live."""
    from pm import paths

    root = Path(paths.writable_store_root())
    if root.is_dir():
        for item in root.glob(".prefetch-*"):
            shutil.rmtree(item, ignore_errors=True)


def _prefetch(name: str, stop: threading.Event, repair: bool = False) -> None:
    """Fill the fetch cache without the store lock; the install then only unpacks and publishes.

    A repair verifies first, so damaged bytes are refetched here and not under the store lock. The
    scratch is not ``Store.scratch``: ``pm gc`` removes ``.staging-*`` as a killed installer's, on
    the rule that those only exist under the store lock this download does not hold.
    """
    from pm.install import _installed_location, _lockfile
    from pm.registry import get_package, walk
    from pm.store import Store, current_target
    from pm import paths

    lockfile, target = _lockfile(), current_target()
    store = Store(paths.writable_store_root())
    last = 0.0

    def tick(done, total, _ranges):
        nonlocal last
        now = time.monotonic()
        if now - last >= _PROGRESS_INTERVAL_SECONDS:
            last = now
            _record(name, done=done, total=total)

    for package in walk([name]):
        if (getattr(package, "pin_only", False)
                or _installed_location(package, lockfile, target, verify=repair) is not None):
            continue
        artifacts = lockfile.artifacts(package.name, target)
        if artifacts:
            store.root.mkdir(parents=True, exist_ok=True)
            scratch = Path(tempfile.mkdtemp(prefix=".prefetch-", dir=store.root))
            try:
                store.fetch_many(artifacts, scratch, progress=tick, pause_event=stop)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)


def _install_one(name: str, repair: bool, stop: threading.Event) -> None:
    from pm.downloader import DownloadError, DownloadPaused
    from pm.install import ensure
    from pm.package import InstallError

    _record(name, status="downloading", version=_version(name), done=0, total=0)
    try:
        with _package_lock(name):
            _prefetch(name, stop, repair)
        _record(name, status="installing")
        # verify re-hashes what is recorded and re-realizes it on a mismatch: that IS the repair.
        ensure(name, explicit=True, verify=repair, pause_event=stop)
    except DownloadPaused:
        _record(name, status="paused")
    except (InstallError, DownloadError, OSError) as exc:
        message = str(exc)

        def failed(state):
            entry = state.setdefault("packages", {}).setdefault(name, {})
            attempts = _entry(state, name).get("attempts", 0) + 1
            entry.update(version=_version(name), attempts=attempts, error=message, status="retrying",
                         next_attempt=time.time() + _backoff(attempts))
            if attempts >= MAX_ATTEMPTS:
                entry["status"] = "failed"
        _update(failed)
        print(f"✗ {name}: {message}", flush=True)
    else:
        def succeeded(state):
            (state.get("packages") or {}).pop(name, None)
            state["repair"] = [item for item in state.get("repair") or [] if item != name]
        _update(succeeded)
        print(f"✓ {name}", flush=True)


# ── Daemon ───────────────────────────────────────────────────────────────────────────────────────

def _watch_pause(stop: threading.Event) -> None:
    while not stop.wait(_CONTROL_POLL_SECONDS):
        if _load().get("paused"):
            stop.set()


def _next_wait(state: dict, names: list[str], now: float) -> float | None:
    """Seconds until the earliest retry; None when only exhausted packages remain."""
    waiting = [_entry(state, name).get("next_attempt", 0) for name in names
               if _entry(state, name).get("attempts", 0) < MAX_ATTEMPTS]
    return max(min(waiting) - now, 0) if waiting else None


def run() -> int:
    """The daemon. Exits when nothing is left that a retry could help."""
    # A grace period: daemon_running() probes hold this lock for an instant, and must not make a
    # starting daemon conclude that another one is already at work.
    fd = lock_path(_directory() / "driver.lock", timeout=1)
    if fd is None:
        return 0
    stop = threading.Event()
    threading.Thread(target=_watch_pause, args=(stop,), daemon=True).start()  # health: allow HX012 -- machine-scoped, reads no profile state
    try:
        _reclaim_prefetch_scratch()
        _update(lambda state: state.update(pid=os.getpid()))
        while not stop.is_set() and allowed():
            state = _load()
            if state.get("paused"):
                break
            names = pending(state)
            now = time.time()
            ready = [name for name in names if _due(state, name, now)]
            if ready:
                _install_one(ready[0], ready[0] in (state.get("repair") or []), stop)
                continue
            delay = _next_wait(state, names, now)
            if delay is None:
                break
            # Short naps, so a repair or a fixed pin takes effect now and not after a long backoff.
            stop.wait(min(delay, _CONTROL_POLL_SECONDS))
    finally:
        _update(lambda state: state.pop("pid", None))
        os.close(fd)
    return 0


def _spawn() -> None:
    from pm.paths import repo_root

    log = _directory() / "daemon.log"
    if log.is_file() and log.stat().st_size > _LOG_LIMIT_BYTES:
        log.unlink()
    def popen(**kwargs):
        with log.open("ab") as output:
            subprocess.Popen([sys.executable, "-m", "pm.cli", "background", "run"], cwd=repo_root(),
                             stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                             close_fds=True, **kwargs)

    if os.name != "nt":
        popen(start_new_session=True)
        return
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        # An update's tail runs in its kill-on-close job; the daemon must outlive that owner.
        popen(creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB)
    except OSError:
        popen(creationflags=flags)  # This job forbids breakaway; the next kick() restarts us if it dies.


def kick() -> bool:
    """Start the daemon if work is due. Cheap, idempotent, and safe from any process's startup."""
    from pm.package import InstallError

    try:
        if not allowed():
            return False
        state = _load()
        now = time.time()
        if state.get("paused") or not any(_due(state, name, now) for name in pending(state)):
            return False
        if daemon_running():
            return False
        _spawn()
        return True
    except (OSError, ValueError, RuntimeError, InstallError) as exc:
        print(f"background install did not start: {exc}", file=sys.stderr)
        return False


# ── Long-lived processes ─────────────────────────────────────────────────────────────────────────

_seen_pending: set[str] | None = None


def tick() -> None:
    """A housekeeping beat: resume after a reboot or a backoff, and pick up finished tools.

    A process activates PATH once at startup, so a tool the daemon finishes later would stay
    invisible to ``shutil.which`` until restart.
    """
    global _seen_pending
    from pm.install import activate

    kick()
    now = set(pending())
    if _seen_pending is not None and _seen_pending - now:
        activate(allow_incomplete=True)
    _seen_pending = now


def watch_from_cli(interval: float = 30) -> None:
    """Same for a CLI session, which has no housekeeping thread; ends with the pending set."""
    from pm.package import InstallError

    kick()
    try:
        initial = pending() if allowed() else []
    except (OSError, ValueError, RuntimeError, InstallError):
        return  # kick() already reported it; startup must go on.
    if not initial:
        return

    def loop():
        global _seen_pending
        _seen_pending = set(initial)
        while _seen_pending:
            time.sleep(interval)
            try:
                tick()
            except (OSError, ValueError, RuntimeError, InstallError):
                return

    threading.Thread(target=loop, daemon=True, name="pm-background-watch").start()  # health: allow HX012 -- machine-scoped PM state


# ── Commands ─────────────────────────────────────────────────────────────────────────────────────

def status() -> dict:
    state = _load()
    names = pending(state)
    running = daemon_running()
    rows = {}
    for name in names:
        entry = _entry(state, name)
        attempts = entry.get("attempts", 0)
        recorded = entry.get("status", "queued")
        if not running and recorded in ("downloading", "installing"):
            recorded = "interrupted"  # The daemon died mid-step; the next start resumes it.
        rows[name] = {"status": "failed" if attempts >= MAX_ATTEMPTS else recorded,
                      "attempts": attempts, "error": entry.get("error"),
                      "done": entry.get("done", 0), "total": entry.get("total", 0),
                      "next_attempt": entry.get("next_attempt")}
    return {"running": running, "paused": bool(state.get("paused")), "allowed": allowed(), "packages": rows}


def _discard_downloads(name: str) -> None:
    """Forget partials and cached archives of ``name``'s chain; refuses while they are in use."""
    from pm.artifact_mirror import pinned_source
    from pm.download_state import partial_lock
    from pm.downloader import Download
    from pm.install import _discard_entry, _lockfile
    from pm.package import InstallError
    from pm.paths import partials_root, writable_store_root
    from pm.registry import walk
    from pm.store import Store, current_target

    root = partials_root()
    keyer = Download([], partials_dir=root)
    store = Store(writable_store_root())
    artifacts = [artifact for package in walk([name])
                 for artifact in _lockfile().artifacts(package.name, current_target())]
    with _package_lock(name, wait=False) as acquired:
        if not acquired:
            raise InstallError(name, "the daemon is downloading it",
                               "run `hermes pm background stop` first, then repair")
        for artifact in artifacts:
            source = pinned_source(artifact["url"], root / "unused", artifact["sha256"])
            for url in (source.url, *source.fallbacks):
                key = keyer._key(url)
                with partial_lock(root, key) as owned:
                    if owned:
                        (root / f"{key}.part").unlink(missing_ok=True)
                        (root / f"{key}.ranges").unlink(missing_ok=True)
    # Outside the package lock: a foreground caller may hold the store lock while waiting on it.
    with store.install_lock():
        for artifact in artifacts:
            _discard_entry(store, f"fetch-{artifact['sha256']}")


def repair(names: list[str], *, fresh: bool = False) -> list[str]:
    """Forget failures, re-verify recorded bytes, optionally drop downloaded bytes, and resume."""
    from pm.package import InstallError

    known = wanted()
    selected = names or known
    unknown = [name for name in selected if name not in known]
    if unknown:
        raise InstallError(unknown[0], f"not a background package here (have: {', '.join(known)})", "none")
    if fresh:
        for name in selected:
            _discard_downloads(name)

    def reset(state):
        state.pop("paused", None)
        rows = state.setdefault("packages", {})
        for name in selected:
            rows.pop(name, None)
        state["repair"] = sorted({*(state.get("repair") or []), *selected})
    _update(reset)
    kick()
    return selected


def cmd_background(args) -> int:
    action = args.action
    if action == "run":
        return run()
    if action == "repair":
        selected = repair(args.names, fresh=args.fresh)
        print(f"repair queued: {', '.join(selected) or 'nothing'}")
        return 0
    if action == "stop":
        _update(lambda state: state.update(paused=True))
        print("stopped; partial downloads are kept. `hermes pm background start` resumes")
        return 0
    if action == "start":
        _update(lambda state: state.pop("paused", None))
        print("started" if kick() else "nothing due")
        return 0
    report = status()
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"daemon: {'running' if report['running'] else 'stopped'}{' (paused)' if report['paused'] else ''}")
    if not report["packages"]:
        print("nothing pending")
    for name, row in report["packages"].items():
        line = f"{name}: {row['status']}"
        if row["total"]:
            line += f" {row['done'] * 100 // row['total']}%"
        if row["error"]:
            line += f" (attempt {row['attempts']}: {row['error']})"
        print(line)
    if any(row["status"] == "failed" for row in report["packages"].values()):
        print("run `hermes pm background repair` to retry failed packages")
    return 0


def add_parser(sub) -> None:
    parser = sub.add_parser("background", help="the daemon that installs large tools (node, ffmpeg, chromium, ...)")
    actions = parser.add_subparsers(dest="action")
    show = actions.add_parser("status", help="show pending packages and the daemon")
    show.add_argument("--json", action="store_true")
    actions.add_parser("start", help="resume, or start the daemon if work is due")
    actions.add_parser("stop", help="stop and stay stopped until start or repair; keeps partial downloads")
    actions.add_parser("run", help="be the daemon (internal; `start` spawns this)")
    fix = actions.add_parser("repair", help="forget failures, re-verify installed bytes and reinstall what is damaged")
    fix.add_argument("names", nargs="*")
    fix.add_argument("--fresh", action="store_true", help="also discard partial downloads and cached archives")
    parser.set_defaults(func=cmd_background, action="status", json=False)
