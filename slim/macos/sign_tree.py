#!/usr/bin/env python3
"""Developer ID-sign and notarize every Mach-O under the given paths (#140).

    python3 slim/macos/sign_tree.py --entitlements slim/macos/entitlements.plist PATH...

Each PATH is a directory (the staged bundle) or a single binary (the zstd helper). Every
loose Mach-O found by slim/macho_signatures.py is signed with `codesign --force --timestamp
--options runtime`, which notarization requires; universal binaries get every slice.
Then all of them go to Apple's notary service in one submission and the run fails unless
it comes back Accepted. Loose binaries cannot carry a stapled ticket; Gatekeeper fetches
the ticket online. Vendor .app bundles (CuaDriver.app) keep their own Developer ID
signature and are only verified, as are binaries already Developer ID signed by their
vendor (Node); see machos().

Environment:
    APPLE_SIGNING_IDENTITY   "Developer ID Application: …" (required)
    APPLE_NOTARY_USER, APPLE_NOTARY_PASSWORD
                             Apple ID and app-specific password for notarytool; the team is
                             the one in the identity's "(TEAMID)". Without them the binaries
                             are signed but not notarized (a warning says so).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import macho_signatures


def inside_app(path: Path) -> bool:
    return any(parent.suffix == ".app" for parent in path.parents)


def machos(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    """(Mach-O files to sign, vendor-signed files and .app bundles to keep as shipped).

    An .app arrives sealed by its vendor (CuaDriver.app: trycua's Developer ID, an
    embedded provisioning profile, entitlements). Re-signing a binary inside it breaks
    the seal, which the notary rejects as "The signature of the binary is invalid", and
    would reset the Accessibility/Screen Recording grants macOS ties to that identity.
    A loose binary that already carries a vendor's Developer ID signature keeps it too:
    Node's carries the entitlements V8 needs under the hardened runtime (allow-jit),
    which ours does not grant (#142).
    """
    found: list[Path] = []
    kept: set[Path] = set()
    for path in paths:
        if path.is_dir():
            for macho, kind in macho_signatures.scan(path).items():
                if inside_app(macho):
                    kept.add(next(p for p in macho.parents if p.suffix == ".app"))
                elif kind == "developer-id":
                    kept.add(macho)
                else:
                    found.append(macho)
        elif macho_signatures.classify(path) is not None:
            found.append(path)
        else:
            raise SystemExit(f"{path} is not a Mach-O binary")
    return found, sorted(kept)


def verify_kept(kept: list[Path]) -> None:
    for path in kept:
        check = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(path)],
                               capture_output=True, text=True, timeout=300, check=False)
        info = subprocess.run(["codesign", "-dv", "--verbose=2", str(path)],
                              capture_output=True, text=True, timeout=60, check=False).stderr
        if check.returncode or "Authority=Developer ID Application" not in info:
            raise SystemExit(f"{path} is not intact and Developer ID signed: {check.stderr.strip()}")
        print(f"kept vendor signature: {path.name}", flush=True)


def sign(files: list[Path], identity: str, entitlements: Path) -> None:
    for path in files:
        subprocess.run(["codesign", "--force", "--timestamp", "--options", "runtime",
                        "--entitlements", str(entitlements), "--sign", identity, str(path)],
                       check=True, timeout=300)
    bad = [p for p in files if subprocess.run(["codesign", "--verify", "--strict", str(p)],
                                              capture_output=True, timeout=60, check=False).returncode]
    if bad:
        raise SystemExit("codesign --verify failed:\n" + "\n".join(map(str, bad)))
    print(f"signed {len(files)} Mach-O binaries as {identity}", flush=True)


def notarize(files: list[Path], auth: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        payload = Path(tmp) / "payload"
        for i, path in enumerate(files):
            # Flat names keep the archive small and avoid recreating the tree.
            dest = payload / f"{i:05d}-{path.name}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        archive = Path(tmp) / "binaries.zip"
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(payload), str(archive)], check=True, timeout=600)
        out = subprocess.run(["xcrun", "notarytool", "submit", str(archive), *auth, "--wait",
                              "--timeout", "30m", "--output-format", "json"],
                             capture_output=True, text=True, timeout=2400, check=False)
        result = json.loads(out.stdout or "{}")
        print(f"notarization {result.get('id')}: {result.get('status')}", flush=True)
        if result.get("status") != "Accepted":
            if result.get("id"):
                log = subprocess.run(["xcrun", "notarytool", "log", result["id"], *auth],
                                     capture_output=True, text=True, timeout=300, check=False)
                print(log.stdout, log.stderr, sep="\n")
            raise SystemExit(f"notarization failed: {out.stderr.strip() or result}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--entitlements", type=Path, required=True)
    ap.add_argument("paths", type=Path, nargs="+")
    args = ap.parse_args()
    identity = os.environ.get("APPLE_SIGNING_IDENTITY", "")
    if not identity.startswith("Developer ID Application"):
        raise SystemExit("APPLE_SIGNING_IDENTITY must name a Developer ID Application identity")
    files, kept = machos(args.paths)
    sign(files, identity, args.entitlements)
    verify_kept(kept)
    user, password = os.environ.get("APPLE_NOTARY_USER", ""), os.environ.get("APPLE_NOTARY_PASSWORD", "")
    team = re.search(r"\(([A-Z0-9]{10})\)$", identity)
    if user and password and team:
        notarize(files, ["--apple-id", user, "--password", password, "--team-id", team.group(1)])
    else:
        print("::warning::APPLE_NOTARY_USER/APPLE_NOTARY_PASSWORD not set: signed but not notarized", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
