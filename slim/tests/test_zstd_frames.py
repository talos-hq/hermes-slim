"""slim/zstd_frames.py writes zstd's seekable format: independent frames that any zstd
decoder reads as one stream, plus a seek table hermes-slim-unpack decodes them in parallel by.
"""

import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import zstd_frames

try:
    from compression import zstd  # Python 3.14+
    HAVE_ZSTD = True
except ImportError:
    HAVE_ZSTD = False


@unittest.skipUnless(HAVE_ZSTD, "needs Python 3.14's compression.zstd")
class SeekableTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        # Uneven length, mixed compressibility: the last frame is short and frames differ.
        self.data = b"".join(os.urandom(50_000) + bytes(70_000) for _ in range(23)) + b"tail"
        (self.root / "src").write_bytes(self.data)

    def write(self, frame):
        frames = zstd_frames.compress(self.root / "src", self.root / "dst", level=3, frame=frame, workers=3)
        return frames, (self.root / "dst").read_bytes()

    def test_any_zstd_decoder_reads_it_as_one_stream(self):
        from compression import zstd

        frames, packed = self.write(256 << 10)
        self.assertGreater(frames, 5)
        self.assertEqual(zstd.decompress(packed), self.data)

    def test_seek_table_lists_every_frame_in_order(self):
        from compression import zstd

        frames, packed = self.write(256 << 10)
        count, descriptor, magic = struct.unpack("<IBI", packed[-9:])
        self.assertEqual((count, descriptor, magic), (frames, 0, zstd_frames.SEEKABLE_MAGIC))
        start = len(packed) - 9 - 8 * count - 8
        skippable, size = struct.unpack("<II", packed[start:start + 8])
        self.assertEqual((skippable, size), (zstd_frames.SKIPPABLE_MAGIC, 8 * count + 9))
        offset, rebuilt = 0, b""
        for i in range(count):
            csize, dsize = struct.unpack("<II", packed[start + 8 + 8 * i:start + 16 + 8 * i])
            chunk = zstd.decompress(packed[offset:offset + csize])
            self.assertEqual(len(chunk), dsize)
            rebuilt += chunk
            offset += csize
        self.assertEqual(offset, start)  # frames end exactly where the table begins
        self.assertEqual(rebuilt, self.data)

    def test_every_frame_carries_a_checksum(self):
        from compression import zstd

        _, packed = self.write(256 << 10)
        # Flip bytes inside the first frame's compressed data, short of the seek table.
        damaged = bytearray(packed)
        for i in range(200, 264):
            damaged[i] ^= 0x5A
        with self.assertRaises(zstd.ZstdError):
            zstd.decompress(bytes(damaged))


if __name__ == "__main__":
    unittest.main()
