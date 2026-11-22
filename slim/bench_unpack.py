#!/usr/bin/env python3
"""Time unpacking a bundle with the release's parallel unpacker against zstd | tar.

    python slim/bench_unpack.py ARCHIVE UNPACKER [--zstd ZSTD] [--rounds 2]

Informational: CI prints the table (and appends it to the step summary) for the runner's
real filesystem and antivirus. On Windows the baseline is what install.ps1 did before the
unpacker: zstd.exe piped through cmd into System32's tar.exe.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def timed(cmd: str | list, *, shell: bool = False) -> float:
    started = time.monotonic()
    subprocess.run(cmd, shell=shell, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)
    return time.monotonic() - started


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("archive", type=Path)
    ap.add_argument("unpacker", type=Path)
    ap.add_argument("--zstd", default="zstd")
    ap.add_argument("--rounds", type=int, default=2)
    args = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="unpack-bench-"))
    windows = os.name == "nt"

    def baseline(out: Path) -> float:
        if windows:
            tar = Path(os.environ["SystemRoot"]) / "System32" / "tar.exe"
            return timed(f'"{args.zstd}" -dcq "{args.archive}" | "{tar}" -xf - -C "{out}"', shell=True)
        return timed(f'"{args.zstd}" -dcq "{args.archive}" | tar -xf - -C "{out}"', shell=True)

    methods = {
        "zstd | tar": baseline,
        "hermes-slim-unpack": lambda out: timed([str(args.unpacker), str(args.archive), str(out)]),
    }
    results: dict[str, list[float]] = {name: [] for name in methods}
    for _ in range(args.rounds):
        for name, run in methods.items():
            out = work / "out"
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir()
            results[name].append(run(out))
    shutil.rmtree(work, ignore_errors=True)
    base = min(results["zstd | tar"])
    lines = [f"| method | best of {args.rounds} | vs zstd \\| tar |", "|---|---|---|"]
    for name, ts in results.items():
        lines.append(f"| {name.replace('|', chr(92) + '|')} | {min(ts):.2f}s | {base / min(ts):.1f}x |")
    table = "\n".join(lines)
    print(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"\n### Unpack {args.archive.name}\n\n{table}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
