"""Bundle-leg selection and honest reporting for .github/workflows/slim-bundle.yml.

GitHub's macOS arm64 pool sometimes gives up on a job without ever assigning a runner: the job
is cancelled about 15 minutes after it is queued, with runner_name "", zero steps and the
annotation "The job was not acquired by Runner of type hosted even after multiple attempts".
The window is GitHub's (three acquisition attempts; measured at 15m01s, 15m02s and 16m54s on
this repository), not a timeout set in this repository. That cancel says nothing about the
code, so the report below names it as a capacity miss and fails only on real build failures.

    python3 slim/ci_legs.py matrix   # EVENT, LABELS, ARM_IMAGE -> $GITHUB_OUTPUT include=
    python3 slim/ci_legs.py report   # GH_REPO, RUN_ID -> step summary; exit 1 on a real failure
"""

import json
import os
import subprocess
import sys

# macos-latest currently points at the macos-26 arm64 image; pinning keeps the image fixed.
DEFAULT_ARM_IMAGE = "macos-15"
OPT_IN_LABEL = "ci:bundle-macos"
NOT_ACQUIRED = "not acquired by Runner"

LEGS = (
    {"target": "win32-x64", "os": "windows-latest"},
    {"target": "darwin-x64", "os": "macos-15-intel"},
    {"target": "darwin-arm64", "os": DEFAULT_ARM_IMAGE},
    {"target": "linux-x64", "os": "ubuntu-latest"},
    {"target": "linux-arm64", "os": "ubuntu-24.04-arm"},
    {"target": "linux-x64-musl", "os": "ubuntu-latest", "container": "alpine:3.22"},
)


def select_legs(event, labels, arm_image):
    """Return (legs, skipped). Unlabelled PRs skip darwin-arm64, and the reason is returned."""
    image = arm_image or DEFAULT_ARM_IMAGE
    legs = [dict(leg, os=image) if leg["target"] == "darwin-arm64" else dict(leg) for leg in LEGS]
    if event == "pull_request" and OPT_IN_LABEL not in labels:
        reason = (f"darwin-arm64 not built on this PR: GitHub's macOS arm64 pool often cancels "
                  f"jobs without assigning a runner. Add the {OPT_IN_LABEL} label to build it.")
        return ([leg for leg in legs if leg["target"] != "darwin-arm64"],
                [{"target": "darwin-arm64", "reason": reason}])
    return legs, []


def classify(jobs, annotations):
    """Sort finished jobs into failed / not_acquired / superseded. annotations: name -> messages."""
    report = {"failed": [], "not_acquired": [], "superseded": []}
    for job in jobs:
        name, conclusion = job["name"], job.get("conclusion")
        if conclusion == "failure" or conclusion == "timed_out":
            report["failed"].append(name)
        elif conclusion == "cancelled":
            never_ran = not job.get("runner_name") and not job.get("steps")
            if not never_ran:
                report["failed"].append(name)
            elif any(NOT_ACQUIRED in msg for msg in annotations.get(name, [])):
                report["not_acquired"].append(name)
            else:
                report["superseded"].append(name)
    return report


def _gh(path, jq):
    out = subprocess.run(["gh", "api", "--paginate", path, "--jq", jq],
                         check=True, capture_output=True, text=True, timeout=120).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _matrix():
    labels = [x for x in os.environ.get("LABELS", "").split(",") if x]
    legs, skipped = select_legs(os.environ.get("EVENT", ""), labels, os.environ.get("ARM_IMAGE", ""))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
        out.write("include=" + json.dumps(legs) + "\n")
    for leg in skipped:
        print(f"::notice title=darwin-arm64 skipped::{leg['reason']}")
    print("bundle legs:", ", ".join(f"{leg['target']}={leg['os']}" for leg in legs))
    return 0


def _report():
    repo, run = os.environ["GH_REPO"], os.environ["RUN_ID"]
    jobs = [j for j in _gh(f"repos/{repo}/actions/runs/{run}/jobs?per_page=100", ".jobs[] | tojson")
            if j["name"].startswith("bundle (")]
    annotations = {}
    for j in jobs:
        if j.get("conclusion") == "cancelled":
            annotations[j["name"]] = _gh(f"repos/{repo}/check-runs/{j['id']}/annotations", ".[].message | tojson")
    report = classify(jobs, annotations)
    lines = ["## Bundle legs", ""]
    for name in report["not_acquired"]:
        print(f"::warning title=Runner not acquired::{name} never got a GitHub-hosted runner "
              "(cancelled with zero steps). This is GitHub capacity, not a build failure.")
        lines.append(f"- RUNNER NOT ACQUIRED (GitHub capacity, no step ran): {name}")
    lines += [f"- superseded before start: {n}" for n in report["superseded"]]
    lines += [f"- BUILD FAILURE: {n}" for n in report["failed"]]
    if not any(report.values()):
        lines.append("- every leg ran and passed")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit({"matrix": _matrix, "report": _report}[sys.argv[1]]())
