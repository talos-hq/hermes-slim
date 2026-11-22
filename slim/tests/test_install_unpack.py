"""install.sh unpacks with the release's hermes-slim-unpack when it can, verified, and falls
back to zstd | tar when it cannot.

hermes-slim-unpack itself is tested in slim/unpack (cargo test); here a stub stands in for
it (it records that it ran, then unpacks with tar), so these tests pin the installer's
contract: where it looks for the unpacker, that a checksum mismatch stops the install, and
that a failing unpacker does not.
"""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import range_server

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"
ASSET = "hermes-slim-fixture-any.tar.zst"


def host_unpacker() -> str | None:
    """The unpacker asset install.sh looks for on this host (its unpack_asset case)."""
    names = {("Linux", "x86_64"): "hermes-slim-unpack-linux-x64", ("Linux", "aarch64"): "hermes-slim-unpack-linux-arm64",
             ("Darwin", "arm64"): "hermes-slim-unpack-darwin-arm64", ("Darwin", "x86_64"): "hermes-slim-unpack-darwin-x64"}
    return names.get((os.uname().sysname, os.uname().machine))


@unittest.skipIf(os.name == "nt" or not host_unpacker(), "install.sh publishes no unpacker for this host")
class UnpackerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.release = self.root / "release"
        self.release.mkdir()
        tree = self.root / "tree" / "hermes-slim" / "bin"
        tree.mkdir(parents=True)
        for command in ("hermes", "hermes-slim-setup"):
            (tree / command).write_text("#!/bin/sh\nexit 0\n")
            (tree / command).chmod(0o755)
        with tarfile.open(self.release / ASSET, "w") as tar:
            tar.add(tree.parent, arcname="hermes-slim")
        digest = hashlib.sha256((self.release / ASSET).read_bytes()).hexdigest()
        (self.release / f"{ASSET}.sha256").write_text(f"{digest}  {ASSET}\n")
        commands = self.root / "commands"
        commands.mkdir()
        (commands / "zstd").write_text('#!/bin/sh\nprintf zstd > "$HOME/zstd-ran"\ncat "$2"\n')
        (commands / "zstd").chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{commands}:{os.environ['PATH']}")
        self.env.pop("HERMES_SLIM_DIR", None)
        self.name = host_unpacker()

    def unpacker(self, exit_code=0, sha=None):
        """A stand-in hermes-slim-unpack: ARCHIVE DEST --strip 1, recorded in $HOME/unpack-ran."""
        script = ('#!/bin/sh\nprintf "%s " "$@" > "$HOME/unpack-ran"\n'
                  f'[ {exit_code} = 0 ] || exit {exit_code}\n'
                  'tar -xf "$1" -C "$2" --strip-components 1 && echo "unpacked 3 files"\n')
        path = self.release / self.name
        path.write_text(script)
        path.chmod(0o755)
        digest = sha or hashlib.sha256(script.encode()).hexdigest()
        (self.release / f"{self.name}.sha256").write_text(f"{digest}  {self.name}\n")

    def install(self, archive):
        return subprocess.run(["sh", str(INSTALLER), "--archive", str(archive), "--no-setup"],
                              env=self.env, capture_output=True, text=True, timeout=120, check=False)

    def installed(self):
        return (self.home / ".hermes-slim" / "current" / "bin" / "hermes").exists()

    def test_unpacker_beside_a_local_archive_unpacks_it(self):
        self.unpacker()
        result = self.install(self.release / ASSET)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.installed())
        self.assertIn("--strip 1", (self.home / "unpack-ran").read_text())
        self.assertFalse((self.home / "zstd-ran").exists(), "zstd | tar ran although the unpacker did the job")

    def test_unpacker_with_a_wrong_checksum_stops_the_install(self):
        self.unpacker(sha="0" * 64)
        result = self.install(self.release / ASSET)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sha256 mismatch", result.stderr)
        self.assertFalse((self.home / "unpack-ran").exists(), "an unverified unpacker ran")
        self.assertFalse(self.installed())

    def test_failing_unpacker_falls_back_to_zstd_and_tar(self):
        self.unpacker(exit_code=3)
        result = self.install(self.release / ASSET)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.home / "unpack-ran").exists())
        self.assertTrue((self.home / "zstd-ran").exists())
        self.assertIn("trying zstd and tar", result.stderr)
        self.assertTrue(self.installed())

    def test_mirror_serving_the_unpacker_gets_it_used(self):
        self.unpacker()
        server = range_server.serve(self.release)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        result = self.install(f"http://127.0.0.1:{server.server_address[1]}/{ASSET}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.home / "unpack-ran").exists())
        self.assertTrue(self.installed())

    def test_without_an_unpacker_zstd_and_tar_unpack_as_before(self):
        result = self.install(self.release / ASSET)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.home / "zstd-ran").exists())
        self.assertTrue(self.installed())


if __name__ == "__main__":
    unittest.main()
