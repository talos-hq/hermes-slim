"""install.sh downloads a bundle over parallel byte ranges and resumes every stage.

A real HTTP server (range_server.py) serves a fixture bundle; only zstd is stubbed,
because compression is outside this contract.
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


@unittest.skipIf(os.name == "nt", "install.sh is the macOS/Linux installer")
class DownloadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.release = self.root / "release"
        self.release.mkdir()
        tree = self.root / "tree" / "hermes-slim"
        (tree / "bin").mkdir(parents=True)
        for command in ("hermes", "hermes-slim-setup"):
            exe = tree / "bin" / command
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
        # Big enough (> 8 MiB) for the installer to split it into ranges.
        (tree / "payload.bin").write_bytes(os.urandom(12 * 2**20))
        with tarfile.open(self.release / ASSET, "w") as tar:
            tar.add(tree, arcname="hermes-slim")
        self.digest = hashlib.sha256((self.release / ASSET).read_bytes()).hexdigest()
        (self.release / f"{ASSET}.sha256").write_text(f"{self.digest}  {ASSET}\n")
        commands = self.root / "commands"
        commands.mkdir()
        zstd = commands / "zstd"
        zstd.write_text('#!/bin/sh\ncat "$2"\n')
        zstd.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{commands}:{os.environ['PATH']}")
        self.env.pop("HERMES_SLIM_DIR", None)

    def serve(self, drop_after=None):
        server = range_server.serve(self.release, drop_after=drop_after)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}/{ASSET}"

    def install(self, url, *extra):
        return subprocess.run(
            ["sh", str(INSTALLER), "--archive", url, "--no-setup", *extra],
            env=self.env, capture_output=True, text=True, timeout=120, check=False,
        )

    def requests(self):
        log = self.release / "requests.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if line.startswith(f"GET /{ASSET} ")]

    def assert_installed(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        current = self.home / ".hermes-slim" / "current"
        self.assertEqual((current / ".bundle-sha256").read_text().strip(), self.digest)
        self.assertEqual(len((current / "payload.bin").read_bytes()), 12 * 2**20)
        self.assertEqual(list((self.home / ".hermes-slim" / "downloads").iterdir()), [])

    def test_downloads_ranges_in_parallel(self):
        result = self.install(self.serve(), "--jobs", "4")
        self.assert_installed(result)
        ranges = [line.split(" ", 2)[2] for line in self.requests()]
        self.assertGreaterEqual(len([r for r in ranges if r and r != "bytes=0-0"]), 4, ranges)

    def test_dropped_connections_resume_from_the_last_byte(self):
        result = self.install(self.serve(drop_after=2**20), "--jobs", "4")
        self.assert_installed(result)
        chunk = (len((self.release / ASSET).read_bytes()) + 3) // 4
        starts = {int(line.rsplit("=", 1)[1].split("-")[0]) for line in self.requests() if "=" in line}
        # Every range was cut at 1 MiB and then requested again from exactly that offset.
        self.assertTrue({i * chunk + 2**20 for i in range(4)} <= starts, starts)

    def test_rerun_continues_a_killed_download(self):
        url = self.serve()
        data = (self.release / ASSET).read_bytes()
        size, jobs = len(data), 4
        chunk = (size + jobs - 1) // jobs
        parts = self.home / ".hermes-slim" / "downloads" / f"{ASSET}.parts"
        parts.mkdir(parents=True)
        (parts / "size").write_text(f"{size}\n")
        # As left by a run killed half-way through each range.
        for i in range(jobs):
            (parts / str(i)).write_bytes(data[i * chunk:i * chunk + chunk // 2])
        result = self.install(url, "--jobs", str(jobs))
        self.assert_installed(result)
        starts = sorted(int(line.rsplit("=", 1)[1].split("-")[0]) for line in self.requests()
                        if "=" in line and not line.endswith("=0-0"))
        self.assertEqual(starts, [i * chunk + chunk // 2 for i in range(jobs)])

    def test_rerun_of_an_installed_bundle_downloads_nothing(self):
        url = self.serve()
        self.assert_installed(self.install(url))
        before = len(self.requests())
        result = self.install(url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already installed", result.stdout)
        # Only the 1-byte size probe of the cached-file check may run, never a body fetch.
        self.assertEqual([r for r in self.requests()[before:] if not r.endswith("=0-0")], [])

    def test_corrupt_download_is_rejected_and_the_install_untouched(self):
        url = self.serve()
        (self.release / f"{ASSET}.sha256").write_text(f"{'0' * 64}  {ASSET}\n")
        result = self.install(url)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("sha256 mismatch", result.stderr)
        self.assertFalse((self.home / ".hermes-slim" / "current").exists())

    def test_single_stream_when_asked_for_one_job(self):
        result = self.install(self.serve(), "--jobs", "1")
        self.assert_installed(result)
        self.assertEqual(len([r for r in self.requests() if not r.endswith("=0-0")]), 1)


if __name__ == "__main__":
    unittest.main()
