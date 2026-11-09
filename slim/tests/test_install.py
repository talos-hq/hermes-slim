"""Exercise the shell installer offline with a tiny fake bundle."""

import os
from pathlib import Path
import subprocess
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
