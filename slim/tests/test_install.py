"""Exercise the shell installer offline with a tiny fake bundle."""

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"


class InstallTests(unittest.TestCase):
    def test_no_setup_links_commands_without_running_setup(self):
        for no_setup in (True, False):
            with self.subTest(no_setup=no_setup), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                home = root / "home"
                home.mkdir()
                bundle = root / "bundle" / "bin"
                bundle.mkdir(parents=True)
                for command in ("hermes", "hermes-slim-setup"):
                    executable = bundle / command
                    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/setup-ran"\n')
                    executable.chmod(0o755)
                archive = root / "bundle.tar.zst"
                with tarfile.open(archive, "w") as tar:
                    tar.add(bundle.parent, arcname="hermes-slim")
                # Compression is outside this contract; let the installer extract a plain tar.
                commands = root / "commands"
                commands.mkdir()
                zstd = commands / "zstd"
                zstd.write_text('#!/bin/sh\ncat "$2"\n')
                zstd.chmod(0o755)
                env = dict(os.environ, HOME=str(home), PATH=f"{commands}:{os.environ['PATH']}")
                env.pop("HERMES_SLIM_DIR", None)
                args = ["sh", str(INSTALLER), "--archive", str(archive)]
                if no_setup:
                    args.append("--no-setup")
                subprocess.run(args, env=env, check=True, capture_output=True, text=True)
                for command in ("hermes", "hermes-slim-setup"):
                    link = home / ".local" / "bin" / command
                    self.assertTrue(link.is_symlink())
                    self.assertEqual(link.resolve(), home / ".hermes-slim/current/bin" / command)
                    self.assertTrue(os.access(link, os.X_OK))
                marker = home / "setup-ran"
                if no_setup:
                    self.assertFalse(marker.exists())
                else:
                    self.assertEqual(marker.read_text(), "--service\n")


class StandaloneZstdTests(unittest.TestCase):
    def test_download_without_host_zstd_checks_the_native_decompressor(self):
        host = os.uname()
        if host.sysname == "Darwin":
            target = {"arm64": "darwin-arm64", "x86_64": "darwin-x64"}[host.machine]
            zstd_asset = f"zstd-{target}"
        elif host.sysname == "Linux" and host.machine == "x86_64":
            target = "linux-x64-musl" if list(Path("/lib").glob("ld-musl-*.so.1")) else "linux-x64"
            zstd_asset = "zstd-linux-x64"
        else:
            self.skipTest("No standalone decompressor ships for this host")

        for corrupt in (False, True):
            with self.subTest(corrupt_checksum=corrupt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                home = root / "home"
                home.mkdir()
                release = root / "release"
                release.mkdir()
                bundle = root / "bundle" / "bin"
                bundle.mkdir(parents=True)
                for command in ("hermes", "hermes-slim-setup"):
                    executable = bundle / command
                    executable.write_text("#!/bin/sh\nexit 0\n")
                    executable.chmod(0o755)
                asset = f"hermes-slim-fixture-{target}.tar.zst"
                with tarfile.open(release / asset, "w") as tar:
                    tar.add(bundle.parent, arcname="hermes-slim")
                helper = release / zstd_asset
                helper.write_text('#!/bin/sh\nprintf ran > "$HOME/decompressor-ran"\ncat "$2"\n')
                for name in (asset, zstd_asset):
                    digest = hashlib.sha256((release / name).read_bytes()).hexdigest()
                    if corrupt and name == zstd_asset:
                        digest = "0" * 64
                    (release / f"{name}.sha256").write_text(f"{digest}  {name}\n")

                commands = root / "commands"
                commands.mkdir()
                tar_command = commands / "tar"
                tar_command.write_text(
                    '#!/bin/sh\nif [ "$1" = --version ]; then echo bsdtar; exit 0; fi\n'
                    'if [ "$2" != - ]; then echo "tar needs external zstd" >&2; exit 1; fi\n'
                    f'exec "{shutil.which("tar")}" "$@"\n'
                )
                tar_command.chmod(0o755)
                curl = commands / "curl"
                curl.write_text(
                    f"#!{sys.executable}\n"
                    "import json, os, shutil, sys\nfrom pathlib import Path\n"
                    "args = sys.argv[1:]\nurl = args[-1]\n"
                    "with (Path(os.environ['HOME']) / 'downloads').open('a') as log:\n"
                    "    log.write(url + '\\n')\n"
                    "if '/releases/tags/' in url:\n"
                    f"    print(json.dumps({{'assets': [{{'name': {asset!r}}}]}}))\n"
                    "else:\n"
                    f"    shutil.copyfile(Path({str(release)!r}) / url.rsplit('/', 1)[-1], args[args.index('-o') + 1])\n"
                )
                curl.chmod(0o755)
                env = dict(os.environ, HOME=str(home), PATH=f"{commands}:{os.environ['PATH']}")
                env.pop("HERMES_SLIM_DIR", None)
                # Hide host zstd discovery while preserving PATH for command wrappers.
                shell = (
                    'command() { if [ "${1:-}" = -v ] && [ "${2:-}" = zstd ]; '
                    'then return 1; fi; builtin command "$@"; }; source "$@"'
                )
                result = subprocess.run(
                    [shutil.which("bash"), "-c", shell, "install-test", str(INSTALLER),
                     "--version", "v-fixture", "--no-setup"],
                    env=env, capture_output=True, text=True, check=False,
                )
                downloads = (home / "downloads").read_text().splitlines()
                base = "https://github.com/talos-hq/hermes-slim/releases/download/v-fixture"
                self.assertIn(f"{base}/{zstd_asset}", downloads, result.stdout + result.stderr)
                self.assertIn(f"{base}/{zstd_asset}.sha256", downloads)
                if corrupt:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("zstd sha256 mismatch", result.stderr)
                    self.assertFalse((home / "decompressor-ran").exists())
                    self.assertFalse((home / ".hermes-slim/current").exists())
                else:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual((home / "decompressor-ran").read_text(), "ran")
                    self.assertTrue((home / ".local/bin/hermes").is_symlink())
