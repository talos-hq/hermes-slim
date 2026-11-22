#!/usr/bin/env python3
"""Write hermes-slim-manifest.txt for a directory of release assets.

    python slim/release_manifest.py DIST --tag vX.Y.Z > DIST/hermes-slim-manifest.txt

A `release TAG` line, then one line per downloadable asset, `KIND TARGET NAME SHA256 SIZE`:

    release v0.1.2
    bundle darwin-arm64 hermes-slim-<commit>-darwin-arm64.tar.zst <sha256> <bytes>
    zstd darwin-arm64 zstd-darwin-arm64 <sha256> <bytes>
    unpack darwin-arm64 hermes-slim-unpack-darwin-arm64 <sha256> <bytes>
    frames darwin-arm64 hermes-slim-<commit>-darwin-arm64.tar.zst.frames <sha256> <bytes>

The installers read it from `releases/latest/download/hermes-slim-manifest.txt`. That
URL is served by GitHub's release CDN, so unlike api.github.com (60 unauthenticated
calls per hour per IP) it does not run out on a shared network. The file is plain
space-separated text so `sh` + `sed` and PowerShell can read it without a JSON parser.

Every asset's `.sha256` file, when present, must agree with the bytes; a mismatch
aborts the release instead of publishing a manifest that disagrees with it.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

MANIFEST = "hermes-slim-manifest.txt"
BUNDLE = re.compile(r"^hermes-slim-[0-9a-zA-Z.]+-(?P<target>[a-z0-9]+-[a-z0-9-]+?)\.(?:tar\.zst|zip)$")
ZSTD = re.compile(r"^zstd-(?P<target>[a-z0-9]+-[a-z0-9-]+?)(?:\.exe)?$")
# The bundle's frame index (slim/zstd_frames.py); pinning it here pins every frame's sha256.
FRAMES = re.compile(r"^hermes-slim-[0-9a-zA-Z.]+-(?P<target>[a-z0-9]+-[a-z0-9-]+?)\.tar\.zst\.frames$")
UNPACK = re.compile(r"^hermes-slim-unpack-(?P<target>[a-z0-9]+-[a-z0-9-]+?)(?:\.exe)?$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def entries(dist: Path) -> list[tuple[str, str, str, str, int]]:
    rows = []
    for path in sorted(dist.iterdir()):
        if not path.is_file():
            continue
        for kind, pattern in (("bundle", BUNDLE), ("zstd", ZSTD), ("unpack", UNPACK), ("frames", FRAMES)):
            m = pattern.match(path.name)
            if not m:
                continue
            digest = sha256(path)
            recorded = path.with_name(f"{path.name}.sha256")
            if recorded.exists() and recorded.read_text().split()[0] != digest:
                raise SystemExit(f"{recorded.name} does not match {path.name}")
            rows.append((kind, m.group("target"), path.name, digest, path.stat().st_size))
            break
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dist", type=Path)
    ap.add_argument("--tag", required=True, help="the release tag; installers pin every download to it")
    args = ap.parse_args()
    rows = entries(args.dist)
    if not any(kind == "bundle" for kind, *_ in rows):
        raise SystemExit(f"no bundles in {args.dist}")
    print(f"release {args.tag}")
    for row in rows:
        print(" ".join(str(x) for x in row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
