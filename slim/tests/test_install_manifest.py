"""install.sh resolves a release from hermes-slim-manifest.txt on the release CDN, so an
exhausted api.github.com rate limit (60 calls per hour per IP) cannot break an install (#146).

curl is replaced by a stub that serves a fixture release by file name, logs every URL, and
answers api.github.com with HTTP 403 the way the real API does once the limit is used up.
"""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_manifest

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"
TAG = "v9.9.9"


def host_target():
    host = os.uname()
    if host.sysname == "Darwin":
        return {"arm64": "darwin-arm64", "x86_64": "darwin-x64"}[host.machine]
    if host.machine == "x86_64":
        return "linux-x64-musl" if list(Path("/lib").glob("ld-musl-*.so.1")) else "linux-x64"
    return "linux-arm64"


@unittest.skipIf(os.name == "nt", "install.sh is the macOS/Linux installer")
class ManifestTests(unittest.TestCase):
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
        self.asset = f"hermes-slim-fixture-{host_target()}.tar.zst"
        with tarfile.open(self.release / self.asset, "w") as tar:
            tar.add(tree.parent, arcname="hermes-slim")
        digest = hashlib.sha256((self.release / self.asset).read_bytes()).hexdigest()
        (self.release / f"{self.asset}.sha256").write_text(f"{digest}  {self.asset}\n")
        commands = self.root / "commands"
        commands.mkdir()
        (commands / "zstd").write_text('#!/bin/sh\ncat "$2"\n')
        (commands / "curl").write_text(
            f"#!{sys.executable}\n"
            "import os, shutil, sys\nfrom pathlib import Path\n"
            "args = sys.argv[1:]\nurl = [a for a in args if a.startswith('http')][-1]\n"
            "with (Path(os.environ['HOME']) / 'urls').open('a') as log:\n"
            "    log.write(url + '\\n')\n"
            "if 'api.github.com' in url:\n"
            "    sys.stderr.write('curl: (22) The requested URL returned error: 403\\n')\n"
            "    sys.exit(22)\n"
            # Like curl's -w: the installer tells "absent" from "unreachable" by status.
            "def status(code):\n"
            "    if '-w' in args:\n"
            "        sys.stdout.write(args[args.index('-w') + 1].replace('%{http_code}', code))\n"
            f"src = Path({str(self.release)!r}) / url.rsplit('/', 1)[-1]\n"
            "if not src.exists():\n"
            "    status('404')\n"
            "    sys.stderr.write('curl: (22) The requested URL returned error: 404\\n')\n"
            "    sys.exit(22)\n"
            "if '-o' in args:\n"
            "    shutil.copyfile(src, args[args.index('-o') + 1])\n"
            "else:\n"
            "    sys.stdout.buffer.write(src.read_bytes())\n"
            "status('200')\n"
        )
        for name in ("zstd", "curl"):
            (commands / name).chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{commands}:{os.environ['PATH']}")
        self.env.pop("HERMES_SLIM_DIR", None)

    def install(self):
        # --jobs 1: the stub curl does not do byte ranges.
        return subprocess.run(["sh", str(INSTALLER), "--no-setup", "--jobs", "1"], env=self.env,
                              capture_output=True, text=True, timeout=120, check=False)

    def urls(self):
        return (self.home / "urls").read_text().splitlines()

    def test_installs_from_the_manifest_while_the_api_is_rate_limited(self):
        lines = [" ".join(str(x) for x in row) for row in release_manifest.entries(self.release)]
        (self.release / release_manifest.MANIFEST).write_text("\n".join([f"release {TAG}", *lines]) + "\n")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.home / ".hermes-slim" / "current" / "bin" / "hermes").exists())
        self.assertFalse([u for u in self.urls() if "api.github.com" in u], self.urls())
        # Every download after the manifest is pinned to the tag it names.
        self.assertIn(f"https://github.com/talos-hq/hermes-slim/releases/download/{TAG}/{self.asset}", self.urls())

    def test_unpacker_listed_in_the_manifest_is_fetched_pinned_to_it(self):
        target = host_target()
        name = f"hermes-slim-unpack-{'linux-x64' if target.startswith('linux-x64') else target}"
        stub = '#!/bin/sh\nprintf ran > "$HOME/unpack-ran"\ntar -xf "$1" -C "$2" --strip-components 1\n'
        (self.release / name).write_text(stub)
        (self.release / name).chmod(0o755)
        # No .sha256 beside it: the pin must come from the manifest.
        lines = [" ".join(str(x) for x in row) for row in release_manifest.entries(self.release)]
        self.assertIn(f"unpack {name.removeprefix('hermes-slim-unpack-')} {name} ", "\n".join(lines))
        (self.release / release_manifest.MANIFEST).write_text("\n".join([f"release {TAG}", *lines]) + "\n")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.home / "unpack-ran").exists(), "the manifest's unpacker did not unpack the bundle")
        base = f"https://github.com/talos-hq/hermes-slim/releases/download/{TAG}"
        self.assertIn(f"{base}/{name}", self.urls())
        self.assertNotIn(f"{base}/{name}.sha256", self.urls())
        self.assertFalse([u for u in self.urls() if "/zstd-" in u], "the zstd helper was fetched although the unpacker replaces it")

    def test_a_rate_limited_api_is_reported_as_such(self):
        result = self.install()  # this release has no manifest, so the API is the fallback
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rate limit", result.stderr)


if __name__ == "__main__":
    unittest.main()
