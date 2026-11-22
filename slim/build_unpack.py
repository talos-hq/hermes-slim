#!/usr/bin/env python3
"""Build hermes-slim-unpack (slim/unpack) for a bundle target, natively, with soldr.

    python slim/build_unpack.py TARGET OUT_DIR

Writes OUT_DIR/hermes-slim-unpack-TARGET (.exe on Windows) and its .sha256. CI signs it
with the bundle (Developer ID + notarization on macOS, Authenticode on Windows) and records
the .sha256 again, so the checksum the installers check describes the signed bytes. macOS builds
target macOS 11 like slim/build_zstd.sh, so the helper runs on every Mac the bundle does.
Linux builds are static musl binaries (linux-x64 serves the glibc and musl bundles alike);
they need a musl C compiler for zstd's C sources (`musl-gcc`, Debian/Ubuntu: musl-tools).
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

CRATE = Path(__file__).resolve().parent / "unpack"
TRIPLES = {
    "darwin-arm64": "aarch64-apple-darwin",
    "darwin-x64": "x86_64-apple-darwin",
    "win32-x64": "x86_64-pc-windows-msvc",
    "win32-arm64": "aarch64-pc-windows-msvc",
    # Static musl: one binary per arch runs on glibc and musl (Alpine) distributions alike.
    "linux-x64": "x86_64-unknown-linux-musl",
    "linux-arm64": "aarch64-unknown-linux-musl",
}
MACOS_MIN = "11.0"


def asset_name(target: str) -> str:
    return f"hermes-slim-unpack-{target}" + (".exe" if target.startswith("win32-") else "")


def macos_minimum(binary: Path) -> str:
    out = subprocess.run(["otool", "-l", str(binary)], capture_output=True, text=True, check=True, timeout=60).stdout
    m = re.search(r"LC_BUILD_VERSION.*?\n\s*minos (\S+)", out, re.DOTALL)
    return m.group(1) if m else ""


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in TRIPLES:
        raise SystemExit(f"usage: build_unpack.py {{{','.join(TRIPLES)}}} OUT_DIR")
    target, out = sys.argv[1], Path(sys.argv[2])
    triple = TRIPLES[target]
    env = dict(os.environ, MACOSX_DEPLOYMENT_TARGET=MACOS_MIN)
    # From the crate directory, so soldr picks up its rust-toolchain.toml.
    subprocess.run(["soldr", "cargo", "build", "--release", "--locked", "--target", triple],
                   cwd=CRATE, env=env, check=True, timeout=3600)
    built = CRATE / "target" / triple / "release" / ("hermes-slim-unpack" + (".exe" if target.startswith("win32-") else ""))
    out.mkdir(parents=True, exist_ok=True)
    dest = out / asset_name(target)
    shutil.copyfile(built, dest)
    dest.chmod(0o755)
    print(subprocess.run([str(dest), "--version"], capture_output=True, text=True, check=True, timeout=60).stdout.strip())
    if target.startswith("linux-"):
        linked = subprocess.run(["file", "-L", str(dest)], capture_output=True, text=True, check=True, timeout=60).stdout
        if "statically linked" not in linked and "static-pie linked" not in linked:
            raise SystemExit(f"{dest.name} is not static: {linked.strip()}")
    if target.startswith("darwin-"):
        minos = macos_minimum(dest)
        if minos != MACOS_MIN:
            raise SystemExit(f"{dest.name} needs macOS {minos}, not {MACOS_MIN}")
    # Signing rewrites the binary, so CI records this again over the signed bytes.
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    dest.with_name(f"{dest.name}.sha256").write_text(f"{digest}  {dest.name}\n")
    print(f"{dest} ({dest.stat().st_size // 1024} KiB) for {target} on {platform.machine()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
