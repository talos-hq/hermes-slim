#!/usr/bin/env python3
"""End-to-end smoke test of a built bundle, the way a client would use it.

    python slim/smoke_test.py dist/hermes-slim-<rev>-<target>.tar.gz

Unpacks into a fresh directory, runs hermes-slim-setup non-interactively
against a throwaway HERMES_HOME, and checks that:
  1. the bundled interpreter imports every compiled dependency,
  2. config.yaml/.env hold the OpenRouter autofill,
  3. the gateway starts and answers /health,
  4. computer use is baked in: the bundled cua-driver meets the runtime contract, and on Linux
     (Xvfb + xterm on PATH) list_windows + a screenshot work through Hermes' tool layer,
  5. Hermes' PM resolves node, npm, ripgrep and py-spy (and git on Windows) inside the bundle's
     pm-store and each runs, so a client never downloads them (#142),
  6. (optional, OPENROUTER_API_KEY set) the model answers a real prompt.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

COMPILED = ["cryptography.hazmat.bindings._rust", "pydantic_core", "jiter", "markupsafe", "charset_normalizer",
            "httptools", "watchfiles", "psutil", "PIL.Image", "pillow_heif", "ruamel.yaml", "websockets",
            "aiohttp", "telegram", "mcp"]
FAKE_KEY = "sk-or-v1-" + "0" * 64


def step(msg: str) -> None:
    print(f"\n=== {msg}", flush=True)


PM_TOOLS = """
import json, os, subprocess, sys, pm
tools = ["node", "npm", "ripgrep", "py-spy"] + (["git"] if os.name == "nt" else [])
for name in tools:
    found = pm.installed_package(name)
    binary = found.binary if found else None
    if binary is None or not binary.resolve().is_relative_to(os.path.realpath(sys.argv[1])):
        sys.exit(f"PM resolves {name} to {binary}, not inside the bundle: a client would download it")
    out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=120, check=True, env=pm.env_for(name))
    print(name, binary.relative_to(os.path.realpath(sys.argv[1])), out.stdout.strip().splitlines()[0])
"""


def pm_tools_smoke(bundle: Path, py: Path, env: dict) -> None:
    # -P: never import a pm from the working directory instead of the bundle's app/.
    subprocess.run([py, "-P", "-c", PM_TOOLS, bundle], env=dict(env, PYTHONPATH=str(bundle / "app")),
                   check=True, timeout=600)


def computer_use_smoke(bundle: Path, py: Path, env: dict) -> None:
    """The baked-in driver meets Hermes' runtime contract (every target that has one); on Linux with
    Xvfb + xterm on PATH, list_windows and a screenshot go through Hermes' computer_use tool layer
    (slim/computer_use_smoke.py) against a private X display. musl bundles carry no driver."""
    script = [py, bundle / "app" / "slim" / "computer_use_smoke.py", "--bundle", bundle]
    run_env = dict(env, PYTHONPATH=str(bundle / "app"), HERMES_SLIM="1")
    if not (bundle / "tools" / "cua-driver").is_dir():
        print("no bundled cua-driver (no build for this target); skipped")
        return
    xvfb, xterm = shutil.which("Xvfb"), shutil.which("xterm")
    if not (sys.platform.startswith("linux") and xvfb and xterm):
        subprocess.run([*script, "--contract-only"], env=run_env, check=True, timeout=300)
        return
    display, title = ":93", "hermes-cua-smoke"
    server = subprocess.Popen([xvfb, display, "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = None
    try:
        x_env = {k: v for k, v in run_env.items() if k not in ("WAYLAND_DISPLAY", "XAUTHORITY")}
        x_env["DISPLAY"] = display
        client = _start_x_client([xterm, "-T", title, "-e", "sleep", "300"], x_env)
        subprocess.run([*script, "--expect-window", title], env=x_env, check=True, timeout=300)
    finally:
        for proc in (client, server):
            if proc is not None:
                proc.terminate()
                proc.wait(timeout=30)


def _start_x_client(argv: list, env: dict) -> subprocess.Popen:
    """Start an X client once the fresh Xvfb accepts connections (a client started first just exits)."""
    deadline = time.monotonic() + 30
    while True:
        proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            return proc  # still running: it connected and mapped its window
        if time.monotonic() > deadline:
            raise AssertionError(f"{argv[0]} could not connect to {env['DISPLAY']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("archive", type=Path)
    ap.add_argument("--port", type=int, default=8642)
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="hermes-slim-smoke-"))
    windows = os.name == "nt"
    bundle = root / "hermes-slim"
    # The release's parallel unpacker (slim/unpack), when this build made one: the installers
    # use it whenever it sits beside a local archive, so the smoke test installs through them.
    unpacker = sorted(args.archive.parent.glob("hermes-slim-unpack-*"))
    unpacker = [u for u in unpacker if not u.name.endswith(".sha256")]
    if args.archive.suffix == ".zip":
        zipfile.ZipFile(args.archive).extractall(root)
    elif windows or unpacker:
        # Unpack the way users do: through the installer, with the release's unpacker (or,
        # on Windows without one, zstd-win32-x64.exe and tar.exe) beside the archive.
        here = Path(__file__).resolve().parent
        # With the unpacker and the bundle's frame index, install the way a release does: over
        # HTTP (slim/tests/range_server.py serving this directory), streamed, so download,
        # frame checks, decode and file writes are exercised together on every OS.
        frames = args.archive.with_name(f"{args.archive.name}.frames")
        stream = bool(unpacker) and frames.exists()
        server = None
        source = str(args.archive)
        if stream:
            # Served from a directory of its own: the server writes its logs where it serves.
            served = root / "served"
            served.mkdir()
            for f in [args.archive, *args.archive.parent.glob(f"{args.archive.name}.*"), *args.archive.parent.glob("hermes-slim-unpack-*")]:
                try:
                    os.link(f, served / f.name)
                except OSError:
                    shutil.copyfile(f, served / f.name)
            server = subprocess.Popen([sys.executable, str(here / "tests" / "range_server.py"), str(served)],
                                      stdout=subprocess.PIPE, text=True)
            source = f"http://127.0.0.1:{int(server.stdout.readline())}/{args.archive.name}"
        if windows:
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(here / "install.ps1"),
                   "-Archive", source, "-Dir", str(root / "install"), "-NoSetup"]
        else:
            cmd = ["sh", str(here / "install.sh"), "--archive", source, "--dir", str(root / "install"), "--no-setup"]
        try:
            installed = subprocess.run(cmd, env=dict(os.environ, HOME=str(root / "install-home")),
                                       capture_output=True, text=True, timeout=1800, check=False)
        finally:
            if server:
                server.terminate()
        print(installed.stdout + installed.stderr, flush=True)
        installed.check_returncode()
        output = installed.stdout + installed.stderr
        if unpacker and "unpacked " not in output:
            raise SystemExit(f"{unpacker[0].name} sits beside the archive but the installer did not unpack with it")
        if stream and "(streamed)" not in output:
            raise SystemExit("the bundle has a frame index but the installer did not stream it")
        bundle = root / "install" / "current"
    else:
        # .tar.zst: GNU tar (with zstd on PATH) and bsdtar both pick the codec from the suffix.
        subprocess.run(["tar", "-xf", args.archive, "-C", root], check=True)
    py = bundle / "python" / ("python.exe" if windows else "bin/python3")
    setup = bundle / "bin" / ("hermes-slim-setup.cmd" if windows else "hermes-slim-setup")
    hermes = bundle / "bin" / ("hermes.cmd" if windows else "hermes")
    home = root / "home"
    real_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    env = {k: v for k, v in os.environ.items() if k not in ("OPENROUTER_API_KEY", "PYTHONPATH", "PYTHONHOME")}
    env.update(HERMES_HOME=str(home), HERMES_NONINTERACTIVE="1")

    step("compiled imports")
    subprocess.run([py, "-c", f"import importlib; [importlib.import_module(m) for m in {COMPILED!r}]; print('ok')"],
                   env=env, check=True)

    step("computer use: bundled cua-driver")
    computer_use_smoke(bundle, py, env)

    step("bundled tools resolve through PM")
    pm_tools_smoke(bundle, py, env)

    step("hermes --version")
    subprocess.run([hermes, "--version"], env=env, check=True, timeout=300)

    step("validation rejects bad input")
    bad = subprocess.run([setup, "--non-interactive", "--first-name", "  ", "--last-name", "X",
                          "--openrouter-key", FAKE_KEY, "--no-gateway", "--json"],
                         env=env, capture_output=True, text=True, check=False)
    assert bad.returncode == 2 and "first name is required" in bad.stderr, bad

    step("non-interactive setup + gateway")
    proc = subprocess.run([setup, "--non-interactive", "--first-name", "  Ada ", "--last-name", " Love lace",
                           "--openrouter-key", real_key or FAKE_KEY, "--api-port", str(args.port), "--json",
                           *([] if real_key else ["--skip-online-checks"])],
                          env=env, capture_output=True, text=True, timeout=600, check=False)
    print(proc.stdout, proc.stderr)
    log = home / "logs" / "slim-gateway.log"
    if proc.returncode and log.exists():
        print(log.read_text(errors="replace")[-8000:])
    assert proc.returncode == 0, "setup failed"
    summary = json.loads(proc.stdout.strip().splitlines()[-1])
    assert summary["first_name"] == "Ada" and summary["last_name"] == "Lovelace", summary
    assert summary["gateway"]["status"] == "ok", summary

    step("config contents")
    cfg = (home / "config.yaml").read_text()
    for needle in ("provider: openrouter", "deepseek/deepseek-v4.1-flash", "https://openrouter.ai/api/v1"):
        assert needle in cfg, f"{needle!r} missing from config.yaml"
    dotenv = (home / ".env").read_text()
    assert "OPENROUTER_API_KEY=" in dotenv and "HERMES_SLIM=1" in dotenv, dotenv

    step("Nous hidden from provider pickers")
    listed = subprocess.run(
        [py, "-c", "from hermes_cli.models_catalog_static import listed_canonical_providers as l; "
                   "print(','.join(getattr(p, 'slug', None) or getattr(p, 'name', None) or str(p) for p in l()))"],
        env=dict(env, PYTHONPATH=str(bundle / "app"), HERMES_SLIM="1"), capture_output=True, text=True, check=True).stdout
    print(listed)
    assert "openrouter" in listed and "nous" not in listed.split(","), listed

    if real_key:
        step("live model round-trip")
        out = subprocess.run([hermes, "-z", "Reply with exactly: PONG"], env=env,
                             capture_output=True, text=True, timeout=300, check=False)
        print(out.stdout[-2000:], out.stderr[-2000:])
        assert "PONG" in out.stdout, "model did not answer"

    step("stop gateway")
    subprocess.run([hermes, "gateway", "stop"], env=env, timeout=120, check=False)
    print("\nSMOKE TEST PASSED")
    shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
