"""Unit tests for slim/setup.py input validation. Stdlib unittest; no Hermes import needed.

    python -m unittest discover -s slim/tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import setup as s


class NameTests(unittest.TestCase):
    def test_trims_and_collapses_whitespace(self):
        self.assertEqual(s.clean_name("  Ada \t", "first name"), "Ada")
        self.assertEqual(s.clean_name(" Mary  Ann ", "first name"), "MaryAnn")

    def test_keeps_apostrophe_and_hyphen(self):
        self.assertEqual(s.clean_name("O'Brien", "last name"), "O'Brien")
        self.assertEqual(s.clean_name("Smith-Jones", "last name"), "Smith-Jones")

    def test_rejects_empty_digits_symbols_and_long(self):
        for bad in ("", "   ", "R2D2", "a@b", "-Ada", "x" * 33):
            with self.assertRaises(s.SetupError, msg=bad):
                s.clean_name(bad, "first name")


class BotUsernameTests(unittest.TestCase):
    def test_format(self):
        self.assertEqual(s.bot_username("Ada", "Lovelace", 1), "hermes_ada_lovelace_1_bot")
        self.assertEqual(s.bot_username("O'Brien", "Smith-Jones", 4), "hermes_obrien_smithjones_4_bot")

    def test_too_long(self):
        with self.assertRaises(s.SetupError):
            s.bot_username("Bartholomew", "Featherstonehaugh", 1)


class KeyTests(unittest.TestCase):
    def test_accepts_and_trims(self):
        key = "sk-or-v1-" + "a" * 40
        self.assertEqual(s.clean_openrouter_key(f"  {key}\n"), key)

    def test_rejects(self):
        for bad in ("", "sk-ant-123456789012345678", "sk-or-short", "sk-or-v1-abc def ghijklmnopqrstuv"):
            with self.assertRaises(s.SetupError, msg=bad):
                s.clean_openrouter_key(bad)


class ArgTests(unittest.TestCase):
    def test_defaults_to_deepseek_flash(self):
        self.assertEqual(s.parse_args([]).model, "deepseek/deepseek-v4.1-flash")

    def test_non_interactive_missing_value_fails(self):
        with self.assertRaises(s.SetupError):
            s.ask("First name", None, lambda v: s.clean_name(v, "first name"), interactive=False)


if __name__ == "__main__":
    unittest.main()
