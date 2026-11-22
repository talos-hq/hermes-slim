#!/usr/bin/env python3
"""Compress a file into independent zstd frames followed by a seek table.

    python slim/zstd_frames.py SRC DST [--level 9] [--frame-mib 8]

The result is zstd's seekable format: ordinary zstd frames of FRAME uncompressed bytes each,
each with a content checksum, then a skippable frame listing every frame's compressed
and decompressed size. Any zstd
decoder reads it as one stream (skippable frames are ignored by definition), so `zstd -d`,
`tar --zstd`, bsdtar and older installers unpack it unchanged; slim/unpack reads the table
and decodes the frames on every core at once.

8 MiB frames cost about 1% in size against one frame at the same level (each frame starts
with an empty window) and leave ~90 frames for a ~730 MiB bundle: enough to keep every
core busy. Frames compress in parallel here too. Needs Python 3.14 (compression.zstd);
build_bundle.py runs it with the bundled interpreter.
"""

from __future__ import annotations

import argparse
import struct
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SKIPPABLE_MAGIC = 0x184D2A5E
SEEKABLE_MAGIC = 0x8F92EAB1


def seek_table(sizes: list[tuple[int, int]]) -> bytes:
    """The skippable frame that ends a seekable archive: (compressed, decompressed) per frame,
    then frame count, a descriptor byte (no per-frame checksums) and the seekable magic."""
    entries = b"".join(struct.pack("<II", c, d) for c, d in sizes)
    footer = struct.pack("<IBI", len(sizes), 0, SEEKABLE_MAGIC)
    return struct.pack("<II", SKIPPABLE_MAGIC, len(entries) + len(footer)) + entries + footer


def compress(src: Path, dst: Path, level: int = 9, frame: int = 8 << 20, workers: int | None = None) -> int:
    """Write DST from SRC; returns the number of frames."""
    from compression import zstd

    # A content checksum per frame (4 bytes): without one, damaged literals can decode
    # "successfully" to wrong bytes, and an --archive without a .sha256 has no other check.
    options = {zstd.CompressionParameter.compression_level: level, zstd.CompressionParameter.checksum_flag: 1}

    def one(chunk: bytes) -> tuple[int, bytes]:
        return len(chunk), zstd.compress(chunk, options=options)

    sizes: list[tuple[int, int]] = []
    with src.open("rb") as f, dst.open("wb") as out, ThreadPoolExecutor(workers) as pool:
        batch = (workers or 8) * 2  # frames in flight: bounds memory at batch x FRAME
        while True:
            chunks = [c for c in (f.read(frame) for _ in range(batch)) if c]
            if not chunks:
                break
            for raw, packed in pool.map(one, chunks):
                out.write(packed)
                sizes.append((len(packed), raw))
        out.write(seek_table(sizes))
    return len(sizes)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path)
    ap.add_argument("--level", type=int, default=9)
    ap.add_argument("--frame-mib", type=int, default=8)
    args = ap.parse_args()
    frames = compress(args.src, args.dst, args.level, args.frame_mib << 20)
    print(f"{args.dst}: {frames} frames")


if __name__ == "__main__":
    main()
