"""The repo passes slim/lint_branding.py, and the lint catches what it should."""

import subprocess
import sys
import unittest
from pathlib import Path

SLIM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SLIM))

import lint_branding as lb


class LintBrandingTests(unittest.TestCase):
    def test_repo_is_clean(self):
        r = subprocess.run([sys.executable, str(SLIM / "lint_branding.py")], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_flags_legacy_and_malformed_names(self):
        for bad in ("install hermes-slim", "HERMES_SLIM=1", "hermes_slim", "Hermes Slim", "Talos-slim",
                    "Talos Slim", "hermes-talos"):
            self.assertTrue(lb.check_text("x.py", bad), bad)

    def test_allows_repo_slug_and_talos(self):
        self.assertEqual(lb.check_text("x.py", "https://github.com/talos-hq/hermes-slim talos-setup ~/.talos"), [])

    def test_compat_allowance_is_per_file(self):
        line = 'os.environ.get("HERMES_SLIM")'
        self.assertEqual(lb.check_text("hermes_cli/setup.py", line), [])
        self.assertTrue(lb.check_text("other.py", line))


if __name__ == "__main__":
    unittest.main()
