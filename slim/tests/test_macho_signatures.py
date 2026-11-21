"""macho_signatures.py tells Developer ID, ad-hoc and unsigned Mach-O binaries apart.

Fixtures are minimal but well-formed Mach-O files built here, so the test runs anywhere.
"""

import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import macho_signatures as ms


def superblob(cms: bytes | None) -> bytes:
    """An embedded-signature superblob with a code directory and, optionally, a CMS blob."""
    slots = [(0, b"\xfa\xde\x0c\x02" + struct.pack(">I", 8))]  # minimal CodeDirectory stub
    if cms is not None:
        slots.append((ms.CSSLOT_SIGNATURESLOT, struct.pack(">II", ms.CSMAGIC_BLOBWRAPPER, 8 + len(cms)) + cms))
    header = 12 + 8 * len(slots)
    index, body, off = b"", b"", header
    for slot, blob in slots:
        index += struct.pack(">II", slot, off)
        body += blob
        off += len(blob)
    return struct.pack(">III", ms.CSMAGIC_EMBEDDED_SIGNATURE, header + len(body), len(slots)) + index + body


def thin(sig: bytes | None) -> bytes:
    """A 64-bit Mach-O header with one load command: LC_CODE_SIGNATURE, or a no-op."""
    data_off = 32 + 16
    if sig is None:
        cmd = struct.pack("<IIII", 0x26, 16, 0, 0)  # LC_FUNCTION_STARTS, empty
    else:
        cmd = struct.pack("<IIII", ms.LC_CODE_SIGNATURE, 16, data_off, len(sig))
    header = struct.pack("<IIIIIIII", ms.MH_MAGIC_64, 0x0100000C, 0, 6, 1, 16, 0, 0)
    return header + cmd + (sig or b"")


def fat(*slices: bytes) -> bytes:
    out = struct.pack(">II", ms.FAT_MAGIC, len(slices))
    offset = 8 + 20 * len(slices)
    offsets = []
    for s in slices:
        offset = (offset + 0xFFF) & ~0xFFF
        offsets.append(offset)
        offset += len(s)
    for s, off in zip(slices, offsets):
        out += struct.pack(">IIIII", 0x0100000C, 0, off, len(s), 12)
    for s, off in zip(slices, offsets):
        out = out.ljust(off, b"\0") + s
    return out


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def kind(self, data: bytes) -> str | None:
        path = self.dir / "bin"
        path.write_bytes(data)
        return ms.classify(path)

    def test_kinds(self):
        self.assertEqual(self.kind(thin(None)), "unsigned")
        self.assertEqual(self.kind(thin(superblob(None))), "adhoc")
        self.assertEqual(self.kind(thin(superblob(b""))), "adhoc")  # empty CMS wrapper
        self.assertEqual(self.kind(thin(superblob(b"\x30\x82certificate-chain"))), "developer-id")

    def test_universal_binary_counts_as_its_weakest_slice(self):
        signed, unsigned = thin(superblob(b"\x30\x82chain")), thin(None)
        self.assertEqual(self.kind(fat(signed, signed)), "developer-id")
        self.assertEqual(self.kind(fat(signed, unsigned)), "unsigned")

    def test_non_macho_files_are_skipped(self):
        self.assertIsNone(self.kind(b"#!/bin/sh\necho hi\n"))
        self.assertIsNone(self.kind(b"\xca\xfe\xba\xbe\x00\x00\x00\x34rest-of-a-java-class"))


if __name__ == "__main__":
    unittest.main()
