#!/usr/bin/env python3
"""Time each phase of a client install of a built bundle.

    python slim/install_bench.py dist/hermes-slim-<rev>-<target>.tar.gz

Phases: unpack, first `hermes --version` (cold: nothing compiled yet), second
one (warm), and non-interactive setup until the gateway answers /health.
Also prints the archive's largest top-level members. Stdlib only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

FAKE_KEY = "sk-or-v1-" + "0" * 64


def timed(label: str, fn) -> float:
    start = time.perf_counter()
    fn()
    took = time.perf_counter() - start
    print(f"{label:<28}{took:8.2f}s", flush=True)
    return took


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("archive", type=Path)
    ap.add_argument("--port", type=int, default=8643)
    args = ap.parse_args()

    print(f"archive {args.archive.name}: {args.archive.stat().st_size / 2**20:.0f} MiB")

    root = Path(tempfile.mkdtemp(prefix="hermes-slim-bench-"))
    bundle = root / "hermes-slim"
    windows = os.name == "nt"
    hermes = bundle / "bin" / ("hermes.cmd" if windows else "hermes")
    setup = bundle / "bin" / ("hermes-slim-setup.cmd" if windows else "hermes-slim-setup")
    home = root / "home"
    env = {k: v for k, v in os.environ.items() if k not in ("OPENROUTER_API_KEY", "PYTHONPATH", "PYTHONHOME")}
    env.update(HERMES_HOME=str(home), HERMES_NONINTERACTIVE="1")

    def unpack() -> None:
        # install.sh unpacks with tar; time the same thing.
        if args.archive.suffix == ".zip":
            zipfile.ZipFile(args.archive).extractall(root)
        else:
            subprocess.run(["tar", "-xf", args.archive, "-C", root], check=True)

    def version() -> None:
        subprocess.run([hermes, "--version"], env=env, check=True, capture_output=True, timeout=300)

    def configure() -> None:
        subprocess.run([setup, "--non-interactive", "--first-name", "Ada", "--last-name", "Lovelace",
                        "--openrouter-key", FAKE_KEY, "--skip-online-checks", "--api-port", str(args.port),
                        "--json"], env=env, check=True, capture_output=True, timeout=600)

    total = timed("unpack", unpack)
    total += timed("hermes --version (cold)", version)
    timed("hermes --version (warm)", version)
    total += timed("setup + gateway /health", configure)
    print(f"{'client install total':<28}{total:8.2f}s")
    subprocess.run([hermes, "gateway", "stop"], env=env, capture_output=True, timeout=120)
    shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
