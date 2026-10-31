#!/usr/bin/env python3
"""End-to-end smoke test of a built bundle, the way a client would use it.

    python slim/smoke_test.py dist/hermes-slim-<rev>-<target>.tar.gz

Unpacks into a fresh directory, runs hermes-slim-setup non-interactively
against a throwaway HERMES_HOME, and checks that:
  1. the bundled interpreter imports every compiled dependency,
  2. config.yaml/.env hold the OpenRouter autofill,
  3. the gateway starts and answers /health,
  4. (optional, OPENROUTER_API_KEY set) the model answers a real prompt.
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
import zipfile
from pathlib import Path

COMPILED = ["cryptography.hazmat.bindings._rust", "pydantic_core", "jiter", "markupsafe", "charset_normalizer",
            "httptools", "watchfiles", "psutil", "PIL.Image", "pillow_heif", "ruamel.yaml", "websockets",
            "aiohttp", "telegram", "mcp"]
FAKE_KEY = "sk-or-v1-" + "0" * 64


def step(msg: str) -> None:
    print(f"\n=== {msg}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("archive", type=Path)
    ap.add_argument("--port", type=int, default=8642)
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="hermes-slim-smoke-"))
    if args.archive.suffix == ".zip":
        zipfile.ZipFile(args.archive).extractall(root)
    else:
        # .tar.zst: GNU tar (with zstd on PATH) and bsdtar both pick the codec from the suffix.
        subprocess.run(["tar", "-xf", args.archive, "-C", root], check=True)
    bundle = root / "hermes-slim"
    windows = os.name == "nt"
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

    step("hermes --version")
    subprocess.run([hermes, "--version"], env=env, check=True, timeout=300)

    step("validation rejects bad input")
    bad = subprocess.run([setup, "--non-interactive", "--first-name", "  ", "--last-name", "X",
                          "--openrouter-key", FAKE_KEY, "--no-gateway", "--json"],
                         env=env, capture_output=True, text=True)
    assert bad.returncode == 2 and "first name is required" in bad.stderr, bad

    step("non-interactive setup + gateway")
    proc = subprocess.run([setup, "--non-interactive", "--first-name", "  Ada ", "--last-name", " Love lace",
                           "--openrouter-key", real_key or FAKE_KEY, "--api-port", str(args.port), "--json",
                           *([] if real_key else ["--skip-online-checks"])],
                          env=env, capture_output=True, text=True, timeout=600)
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
                             capture_output=True, text=True, timeout=300)
        print(out.stdout[-2000:], out.stderr[-2000:])
        assert "PONG" in out.stdout, "model did not answer"

    step("stop gateway")
    subprocess.run([hermes, "gateway", "stop"], env=env, timeout=120)
    print("\nSMOKE TEST PASSED")
    shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
