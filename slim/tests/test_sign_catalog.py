"""The Authenticode signing catalog survives the signing action's Resolve-Path.

azure/artifact-signing-action reads each line of unsigned.txt with Resolve-Path, which
treats [ ] * ? as wildcards. PortableGit ships usr\\bin\\[.exe, and its raw line failed
the release signing step ("The specified wildcard character pattern is not valid").
"""

import sys
import unittest
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_bundle


class CatalogEntryTests(unittest.TestCase):
    def test_wildcard_characters_are_escaped(self):
        rel = PureWindowsPath(r"hermes-slim\pm-store\git\usr\bin\[.exe")
        self.assertEqual(build_bundle.catalog_entry(rel), r"hermes-slim\pm-store\git\usr\bin\`[.exe")
        self.assertEqual(build_bundle.catalog_entry(PureWindowsPath(r"a\[[.exe")), r"a\`[`[.exe")
        self.assertEqual(build_bundle.catalog_entry(PureWindowsPath(r"a\x]y.dll")), r"a\x`]y.dll")

    def test_ordinary_paths_are_unchanged(self):
        rel = PureWindowsPath(r"hermes-slim\python\Lib\site-packages\_cffi_backend.cp314-win_amd64.pyd")
        self.assertEqual(build_bundle.catalog_entry(rel), str(rel))


if __name__ == "__main__":
    unittest.main()
