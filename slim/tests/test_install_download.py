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

    def serve(self, drop_after=None, delay=0.0):
        server = range_server.serve(self.release, drop_after=drop_after, delay=delay)
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

    def test_ranges_are_in_flight_at_the_same_time(self):
        # Each response waits 0.3 s, so ranges overlap if and only if they were requested together.
        result = self.install(self.serve(delay=0.3), "--jobs", "4")
        self.assert_installed(result)
        spans = []
        for line in (self.release / "timing.log").read_text().splitlines():
            start, end, method, path, rng = line.split(" ", 4)
            if method == "GET" and path == f"/{ASSET}" and rng and rng != "bytes=0-0":
                spans.append((float(start), float(end)))
        # Some range must start before another one has finished.
        concurrent = any(a[0] < b[1] and b[0] < a[1] for i, a in enumerate(spans) for b in spans[i + 1:])
        self.assertTrue(concurrent, spans)

    def timeline(self):
        rows = []
        for line in (self.release / "timing.log").read_text().splitlines():
            start, end, method, path, rng = line.split(" ", 4)
            if method == "GET":
                rows.append((float(start), float(end), path.lstrip("/"), rng))
        return rows

    def test_zstd_helper_downloads_while_the_bundle_does(self):
        # The helper install.sh picks for this host (its zstd_asset case).
        helpers = {("Linux", "x86_64"): "zstd-linux-x64", ("Darwin", "arm64"): "zstd-darwin-arm64",
                   ("Darwin", "x86_64"): "zstd-darwin-x64"}
        name = helpers.get((os.uname().sysname, os.uname().machine))
        if not name:
            self.skipTest("install.sh publishes no zstd helper for this host")
        stub = '#!/bin/sh\ncat "$2"\n'
        (self.release / name).write_text(stub)
        (self.release / f"{name}.sha256").write_text(f"{hashlib.sha256(stub.encode()).hexdigest()}  {name}\n")
        # The host PATH with every directory that holds a zstd swapped for a copy without it,
        # so the installer needs its helper and every other command still resolves.
        path = []
        for i, d in enumerate(os.environ["PATH"].split(os.pathsep)):
            if not (Path(d) / "zstd").exists():
                path.append(d)
                continue
            farm = self.root / f"no-zstd-{i}"
            farm.mkdir()
            for exe in Path(d).iterdir():
                if exe.name != "zstd":
                    (farm / exe.name).symlink_to(exe)
            path.append(str(farm))
        self.env["PATH"] = os.pathsep.join(path)
        result = self.install(self.serve(), "--jobs", "4")
        self.assert_installed(result)
        rows = self.timeline()
        bundle_done = max(end for start, end, path, rng in rows if path == ASSET and rng and rng != "bytes=0-0")
        helper_start = min(start for start, end, path, rng in rows if path == name)
        # The helper must not queue behind the bundle: it starts before the last range ends.
        self.assertLess(helper_start, bundle_done, rows)

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
