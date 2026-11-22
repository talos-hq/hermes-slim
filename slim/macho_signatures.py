#!/usr/bin/env python3
"""Classify every Mach-O binary under a directory by its code signature.

    python slim/macho_signatures.py DIR [--fail-unsigned] [--list]

A binary is `developer-id` when its signature carries a CMS blob (a certificate
chain; Apple's notary and Gatekeeper need this), `adhoc` when it is signed without
one (what the linker and python-build-standalone produce), and `unsigned` when it
has no LC_CODE_SIGNATURE. A universal binary counts as its weakest slice.

Stdlib only, runs anywhere, so a Linux job can check a macOS bundle. On a Mac,
`codesign --verify --strict` stays the authority on whether a signature is valid;
this script checks that one is there.
"""

from __future__ import annotations

import argparse
import struct
import sys
from collections import Counter
from pathlib import Path

MH_MAGIC_64 = 0xFEEDFACF  # little-endian thin 64-bit (arm64, x86_64)
MH_MAGIC = 0xFEEDFACE
FAT_MAGIC = 0xCAFEBABE  # big-endian
FAT_MAGIC_64 = 0xCAFEBABF
LC_CODE_SIGNATURE = 0x1D
CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
CSMAGIC_BLOBWRAPPER = 0xFADE0B01  # the CMS signature slot
CSSLOT_SIGNATURESLOT = 0x10000
RANK = {"unsigned": 0, "adhoc": 1, "developer-id": 2}


def _thin_signature(data: bytes, off: int) -> str:
    magic = struct.unpack_from("<I", data, off)[0]
    if magic not in (MH_MAGIC_64, MH_MAGIC):
        return "unsigned"
    ncmds = struct.unpack_from("<I", data, off + 16)[0]
    cmd_off = off + (32 if magic == MH_MAGIC_64 else 28)
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, cmd_off)
        if cmd == LC_CODE_SIGNATURE:
            sig_off, sig_size = struct.unpack_from("<II", data, cmd_off + 8)
            return _blob_kind(data, off + sig_off, sig_size)
        cmd_off += size
    return "unsigned"


def _blob_kind(data: bytes, start: int, size: int) -> str:
    if size < 12 or struct.unpack_from(">I", data, start)[0] != CSMAGIC_EMBEDDED_SIGNATURE:
        return "unsigned"
    count = struct.unpack_from(">I", data, start + 8)[0]
    for i in range(count):
        slot, blob_off = struct.unpack_from(">II", data, start + 12 + 8 * i)
        if slot != CSSLOT_SIGNATURESLOT:
            continue
        magic, length = struct.unpack_from(">II", data, start + blob_off)
        # An ad-hoc signature may still carry an empty wrapper (length 8).
        if magic == CSMAGIC_BLOBWRAPPER and length > 8:
            return "developer-id"
    return "adhoc"


def classify(path: Path) -> str | None:
    """The signature kind of a Mach-O file, or None when it is not Mach-O."""
    with path.open("rb") as f:
        head = f.read(8)
    if len(head) < 8:
        return None
    le, be = struct.unpack_from("<I", head)[0], struct.unpack_from(">I", head)[0]
    if le not in (MH_MAGIC_64, MH_MAGIC) and be not in (FAT_MAGIC, FAT_MAGIC_64):
        return None
    data = path.read_bytes()
    if le in (MH_MAGIC_64, MH_MAGIC):
        return _thin_signature(data, 0)
    # Java class files share 0xCAFEBABE; a real fat header has a handful of arches.
    nfat = struct.unpack_from(">I", data, 4)[0]
    if not 0 < nfat < 16:
        return None
    wide = be == FAT_MAGIC_64
    kinds = []
    for i in range(nfat):
        if wide:
            offset = struct.unpack_from(">Q", data, 8 + 32 * i + 8)[0]
        else:
            offset = struct.unpack_from(">I", data, 8 + 20 * i + 8)[0]
        kinds.append(_thin_signature(data, offset))
    return min(kinds, key=RANK.__getitem__)


def scan(root: Path) -> dict[Path, str]:
    found = {}
    for path in [root] if root.is_file() else sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            kind = classify(path)
            if kind is not None:
                found[path] = kind
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", type=Path)
    ap.add_argument("--fail-unsigned", action="store_true", help="exit 1 unless every binary is developer-id signed")
    ap.add_argument("--list", action="store_true", help="print every binary that is not developer-id signed")
    args = ap.parse_args()
    found = scan(args.root)
    counts = Counter(found.values())
    weak = [p for p, k in found.items() if k != "developer-id"]
    if args.list:
        for p in weak:
            print(f"{found[p]:>9}  {p.relative_to(args.root)}")
    print(", ".join(f"{counts[k]} {k}" for k in ("developer-id", "adhoc", "unsigned")))
    return 1 if args.fail_unsigned and weak else 0


if __name__ == "__main__":
    sys.exit(main())
