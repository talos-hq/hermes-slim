"""Behaviour of slim/ci_legs.py: bundle-leg selection and runner-acquisition classification.

    python -m unittest discover -s slim/tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ci_legs as c

NOT_ACQUIRED = "The job was not acquired by Runner of type hosted even after multiple attempts"


def job(name, conclusion, runner="", steps=0):
    return {"name": name, "conclusion": conclusion, "runner_name": runner,
            "steps": [{}] * steps}


class LegTests(unittest.TestCase):
    def targets(self, legs):
        return {leg["target"]: leg["os"] for leg in legs}

    def test_push_builds_every_leg_on_the_pinned_arm_image(self):
        legs, skipped = c.select_legs("push", [], "")
        self.assertEqual(len(legs), 6)
        self.assertEqual(self.targets(legs)["darwin-arm64"], c.DEFAULT_ARM_IMAGE)
        self.assertNotEqual(c.DEFAULT_ARM_IMAGE, "macos-latest")
        self.assertEqual(skipped, [])

    def test_pr_skips_arm_and_says_so(self):
        legs, skipped = c.select_legs("pull_request", [], "")
        self.assertNotIn("darwin-arm64", self.targets(legs))
        self.assertEqual([s["target"] for s in skipped], ["darwin-arm64"])
        self.assertIn("ci:bundle-macos", skipped[0]["reason"])

    def test_label_or_dispatch_image_opts_back_in(self):
        legs, _ = c.select_legs("pull_request", ["ci:bundle-macos"], "")
        self.assertIn("darwin-arm64", self.targets(legs))
        legs, _ = c.select_legs("workflow_dispatch", [], "macos-26")
        self.assertEqual(self.targets(legs)["darwin-arm64"], "macos-26")


class ClassifyTests(unittest.TestCase):
    def test_zero_step_not_acquired_cancel_is_capacity_not_failure(self):
        jobs = [job("bundle (darwin-arm64, macos-15)", "cancelled"),
                job("bundle (linux-x64, ubuntu-latest)", "success", "GitHub Actions 1", 16)]
        report = c.classify(jobs, {"bundle (darwin-arm64, macos-15)": [NOT_ACQUIRED]})
        self.assertEqual(report["not_acquired"], ["bundle (darwin-arm64, macos-15)"])
        self.assertEqual(report["failed"], [])

    def test_real_failure_and_cancel_after_steps_still_fail(self):
        jobs = [job("bundle (win32-x64, windows-latest)", "failure", "GitHub Actions 2", 9),
                job("bundle (darwin-x64, macos-15-intel)", "cancelled", "GitHub Actions 3", 4)]
        report = c.classify(jobs, {})
        self.assertEqual(sorted(report["failed"]), sorted(j["name"] for j in jobs))
        self.assertEqual(report["not_acquired"], [])

    def test_zero_step_cancel_without_the_annotation_is_not_excused(self):
        report = c.classify([job("bundle (darwin-arm64, macos-15)", "cancelled")],
                            {"bundle (darwin-arm64, macos-15)": ["Canceling since a higher priority waiting request exists"]})
        self.assertEqual(report["not_acquired"], [])
        self.assertEqual(report["superseded"], ["bundle (darwin-arm64, macos-15)"])


if __name__ == "__main__":
    unittest.main()
