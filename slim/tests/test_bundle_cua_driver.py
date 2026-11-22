"""Computer use is baked into the bundle: build_bundle.stage_cua_driver puts the pm/lock.json
cua-driver pin where Hermes' resolver looks (tools/cua-driver/), verified by sha256.

Archives here are synthetic stand-ins with the release layouts; no network.
"""

import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_bundle


def _tar_gz(path: Path, files: dict) -> Path:
    with tarfile.open(path, "w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755 if name.endswith("/cua-driver") else 0o644
            t.addfile(info, io.BytesIO(data))
    return path


class StageCuaDriver(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hs-cua-"))
        self.downloads = self.tmp / "downloads"
        self.downloads.mkdir()
        self.bundle = self.tmp / "hermes-slim"
        self.bundle.mkdir()

    def _stage(self, target: str, archive: Path):
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        repo = self.tmp / "repo"
        (repo / "pm").mkdir(parents=True, exist_ok=True)
        url = f"https://example.invalid/{archive.name}"
        (repo / "pm" / "lock.json").write_text(json.dumps({"packages": {"cua-driver": {
            "artifacts": {target: {"url": url, "sha256": digest}}, "version": "0.57.1"}}}))

        def fake_fetch(u, sha256, dest_dir):
            self.assertEqual((u, sha256), (url, digest))  # the lock's pin, never another URL or hash
            return archive

        with mock.patch.object(build_bundle, "REPO", repo), mock.patch.object(build_bundle, "fetch", fake_fetch):
            return build_bundle.stage_cua_driver(self.bundle, target, self.downloads)

    def test_linux_driver_lands_where_hermes_resolves_it_with_the_wayland_helper(self):
        archive = _tar_gz(self.tmp / "cua-linux.tar.gz", {
            "computer-use-0.57.1-linux-x86_64/cua-driver": b"\x7fELF",
            "computer-use-0.57.1-linux-x86_64/wayland-helper/install.sh": b"x",
        })
        binary = self._stage("linux-x64", archive)
        root = self.bundle / "tools" / "cua-driver"
        self.assertEqual(binary, root / "cua-driver")
        self.assertTrue(os.access(binary, os.X_OK))
        self.assertEqual(sorted(p.name for p in root.iterdir()), ["cua-driver", "wayland-helper"])

    def test_macos_keeps_the_signed_app_bundle_intact(self):
        archive = _tar_gz(self.tmp / "cua-darwin.tar.gz", {
            "ComputerUse.app/Contents/MacOS/cua-driver": b"\xcf\xfa", "ComputerUse.app/Contents/Info.plist": b"x",
            "ComputerUse.app/Contents/_CodeSignature/CodeResources": b"x"})
        binary = self._stage("darwin-arm64", archive)
        self.assertEqual(binary, self.bundle / "tools" / "cua-driver" / "ComputerUse.app" / "Contents" / "MacOS" / "cua-driver")
        self.assertTrue((binary.parents[1] / "_CodeSignature" / "CodeResources").is_file())

    def test_target_without_a_build_ships_no_driver(self):
        archive = _tar_gz(self.tmp / "unused.tar.gz", {"cua-driver": b"x"})
        self.assertIsNone(self._stage_without_pin("linux-x64-musl", archive))
        self.assertFalse((self.bundle / "tools" / "cua-driver").exists())

    def _stage_without_pin(self, target, archive):
        repo = self.tmp / "repo-nopin"
        (repo / "pm").mkdir(parents=True)
        (repo / "pm" / "lock.json").write_text(json.dumps({"packages": {"cua-driver": {
            "artifacts": {"linux-x64": {"url": "u", "sha256": "s"}}}}}))
        with mock.patch.object(build_bundle, "REPO", repo), \
                mock.patch.object(build_bundle, "fetch", side_effect=AssertionError("no fetch for a gap")):
            return build_bundle.stage_cua_driver(self.bundle, target, self.downloads)

    def test_archive_without_the_binary_fails_the_build(self):
        archive = _tar_gz(self.tmp / "broken.tar.gz", {"dir/README": b"x"})
        with self.assertRaises(SystemExit):
            self._stage("linux-x64", archive)

    def test_real_lock_pins_every_glibc_and_desktop_target(self):
        lock = json.loads((build_bundle.REPO / "pm" / "lock.json").read_text())
        pinned = lock["packages"]["cua-driver"]["artifacts"]
        for target in ("linux-x64", "linux-arm64", "darwin-x64", "darwin-arm64", "win32-x64", "win32-arm64"):
            self.assertIn(target, pinned)
            self.assertRegex(pinned[target]["sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
