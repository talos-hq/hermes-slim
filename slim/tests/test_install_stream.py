"""install.sh streams a release that publishes a frame index: hermes-slim-unpack --url
downloads, verifies and unpacks in one pass, pinned to the manifest's sha256s, and any
failure falls back to downloading the bundle and then unpacking it.

The helper itself (frame ordering, per-frame checks, resume) is tested in slim/unpack
(`cargo test`, src/stream_tests.rs); a stub stands in for it here and records how the
installer called it.
"""

import hashlib
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_manifest
import test_install_manifest as manifest_tests

TAG = manifest_tests.TAG
BASE = f"https://github.com/talos-hq/hermes-slim/releases/download/{TAG}"


@unittest.skipIf(os.name == "nt", "install.sh is the macOS/Linux installer")
class StreamTests(unittest.TestCase):
    # The manifest tests' fixture: a release dir served by a stub curl that logs every URL.
    setUp = manifest_tests.ManifestTests.setUp
    install = manifest_tests.ManifestTests.install
    urls = manifest_tests.ManifestTests.urls

    def publish(self, frames=True):
        target = manifest_tests.host_target()
        name = f"hermes-slim-unpack-{'linux-x64' if target.startswith('linux-x64') else target}"
        # --url mode unpacks the release's copy (what the real helper would download).
        stub = ('#!/bin/sh\nprintf "%s " "$@" > "$HOME/unpack-args"\n'
                'if [ "$1" = --url ]; then\n'
                '  [ -f "$HOME/stream-fails" ] && exit 1\n'
                '  url="$2"; shift 2\n'
                '  while [ $# -gt 0 ]; do case "$1" in --index-sha|--sha256|--strip) shift 2 ;; *) dest="$1"; shift ;; esac; done\n'
                f'  tar -xf "{self.release}/${{url##*/}}" -C "$dest" --strip-components 1 && : > "$HOME/streamed"\n'
                'else\n'
                '  tar -xf "$1" -C "$2" --strip-components 1 && : > "$HOME/unpacked-file"\n'
                'fi\n')
        (self.release / name).write_text(stub)
        (self.release / name).chmod(0o755)
        if frames:
            (self.release / f"{self.asset}.frames").write_text("hermes-slim-frames 1\n(stub)\n")
        rows = release_manifest.entries(self.release)
        (self.release / release_manifest.MANIFEST).write_text(
            "\n".join([f"release {TAG}", *(" ".join(str(x) for x in row) for row in rows)]) + "\n")
        return {kind: sha for kind, _target, _name, sha, _size in rows}

    def installed(self):
        return (self.home / ".hermes-slim" / "current" / "bin" / "hermes").exists()

    def test_a_release_with_a_frame_index_streams_the_bundle_pinned_to_the_manifest(self):
        pins = self.publish()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.home / "streamed").exists())
        self.assertTrue(self.installed())
        dest = self.home / ".hermes-slim" / "hermes-slim.new"
        self.assertEqual((self.home / "unpack-args").read_text().split(), [
            "--url", f"{BASE}/{self.asset}", "--index-sha", pins["frames"], "--sha256", pins["bundle"],
            str(dest), "--strip", "1"])
        self.assertNotIn(f"{BASE}/{self.asset}", self.urls(), "the installer downloaded the bundle as well")
        recorded = (self.home / ".hermes-slim" / "current" / ".bundle-sha256").read_text().strip()
        self.assertEqual(recorded, hashlib.sha256((self.release / self.asset).read_bytes()).hexdigest())

    def test_a_stream_that_fails_falls_back_to_download_then_unpack(self):
        self.publish()
        (self.home / "stream-fails").touch()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("downloading the bundle first instead", result.stderr)
        self.assertIn(f"{BASE}/{self.asset}", self.urls())
        self.assertTrue((self.home / "unpacked-file").exists(), "the fallback did not unpack the download")
        self.assertTrue(self.installed())

    def test_a_release_without_a_frame_index_downloads_first(self):
        self.publish(frames=False)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("--url", (self.home / "unpack-args").read_text())
        self.assertIn(f"{BASE}/{self.asset}", self.urls())
        self.assertTrue(self.installed())


if __name__ == "__main__":
    unittest.main()
