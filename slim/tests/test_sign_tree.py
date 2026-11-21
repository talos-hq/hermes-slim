"""sign_tree.py signs loose Mach-O files and leaves vendor-sealed .app bundles alone.

Re-signing a binary inside CuaDriver.app broke its seal: Apple's notary rejected it as
"The signature of the binary is invalid" (#148).
"""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SLIM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SLIM / "tests"))
from test_macho_signatures import thin

spec = importlib.util.spec_from_file_location("sign_tree", SLIM / "macos" / "sign_tree.py")
sign_tree = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sign_tree)


class MachoSplitTests(unittest.TestCase):
    def test_app_bundles_are_kept_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "python" / "lib").mkdir(parents=True)
            (root / "python" / "lib" / "ext.so").write_bytes(thin(None))
            exe = root / "tools" / "cua-driver" / "CuaDriver.app" / "Contents" / "MacOS"
            exe.mkdir(parents=True)
            (exe / "cua-driver").write_bytes(thin(None))
            (root / "README").write_text("not a binary")
            files, apps = sign_tree.machos([root])
            self.assertEqual(files, [root / "python" / "lib" / "ext.so"])
            self.assertEqual(apps, [root / "tools" / "cua-driver" / "CuaDriver.app"])


if __name__ == "__main__":
    unittest.main()
