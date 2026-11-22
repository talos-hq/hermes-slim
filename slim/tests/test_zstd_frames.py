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


class FrameIndexTests(unittest.TestCase):
    """DST.frames: what hermes-slim-unpack --url checks each downloaded frame against."""

    def test_index_lists_every_frame_and_the_seek_table_with_their_hashes(self):
        import hashlib

        frames = [b"frame-one", b"frame-two!", b"3"]
        table = zstd_frames.seek_table([(len(f), 100 * (i + 1)) for i, f in enumerate(frames)])
        data = b"".join(frames) + table
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "b.tar.zst"
            archive.write_bytes(data)
            lines = zstd_frames.frame_index(archive).splitlines()
        def sha(b):
            return hashlib.sha256(b).hexdigest()
        self.assertEqual(lines, [
            "hermes-slim-frames 1",
            f"archive {len(data)} {sha(data)}",
            *(f"frame {len(f)} {100 * (i + 1)} {sha(f)}" for i, f in enumerate(frames)),
            f"tail {len(table)} {sha(table)}",
        ])

    def test_an_archive_without_a_seek_table_has_no_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "b.tar.zst"
            archive.write_bytes(b"\x28\xb5\x2f\xfd" + bytes(64))
            with self.assertRaises(ValueError):
                zstd_frames.frame_index(archive)

    @unittest.skipUnless(HAVE_ZSTD, "needs Python 3.14's compression.zstd")
    def test_compress_writes_the_index_beside_the_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").write_bytes(os.urandom(300_000))
            frames = zstd_frames.compress(root / "src", root / "dst", level=3, frame=64 << 10)
            index = (root / "dst.frames").read_text()
            self.assertEqual(index, zstd_frames.frame_index(root / "dst"))
            self.assertEqual(sum(line.startswith("frame ") for line in index.splitlines()), frames)
