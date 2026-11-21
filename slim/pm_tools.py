"""Prebuild the bundle's pinned tools into <bundle>/pm-store with PM's own installer (#142).

    <bundle python> slim/pm_tools.py stage  BUNDLE TARGET   # build time, native host only
    <bundle python> slim/pm_tools.py reseal BUNDLE          # after code signing

`stage` runs pm.prepare_tools for TOOLS[target] into BUNDLE/pm-store, so the entries,
facts and environment are exactly what `pm.ensure` would produce on the client. It then
keeps only the shipped packages (prepare_tools walks dependencies: ripgrep declares
python for win32-arm64's vcruntime140.dll, and the bundle already ships its own Python)
and drops the download cache. The install stamp names the store (`bundledStore`), and
pm.paths.bundled_store_root reads it as a read-only root.

Signing rewrites binaries inside the entries. `reseal` re-records each entry's digest so
an explicit `hermes pm install` still verifies the shipped (signed) bytes instead of
replacing them with the unsigned upstream download.

Run with the bundle's interpreter and PYTHONPATH=BUNDLE/app; it needs a native build
(npm's install step runs the target's own node).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# git on POSIX is the system's by PM's design (pm/packages.py Git: "POSIX uses system git
# by choice"), so only Windows ships it. Chromium and ffmpeg stay on demand: 100+ MB each
# for features most installs never use.
TOOLS = ("node", "npm", "ripgrep")
WINDOWS_TOOLS = ("git",)
STORE = "pm-store"


def tools(target: str) -> tuple[str, ...]:
    return TOOLS + (WINDOWS_TOOLS if target.startswith("win32") else ())


def stage(bundle: Path, target: str) -> Path:
    store = bundle / STORE
    shutil.rmtree(store, ignore_errors=True)
    # npm's install step finds node through PM's live store: make the build store the live one.
    os.environ["HERMES_RUNTIME_DIR"] = str(store)
    import pm
    from pm.lock import Facts

    names = tools(target)
    pm.prepare_tools(list(names), out=store, target=target)
    facts = Facts(store / "facts.json", strict=True)
    facts.retain(set(names))
    keep = facts.entries_in_use()
    for entry in store.iterdir():
        if entry.is_dir() and entry.name not in keep:
            shutil.rmtree(entry)
    for leftover in store.iterdir():
        if leftover.is_file() and leftover.name != "facts.json":
            leftover.unlink()
    print(f"pm-store: {', '.join(sorted(keep))}", flush=True)
    return store


def reseal(bundle: Path) -> None:
    from pm.lock import Facts

    store = bundle / STORE
    count = Facts(store / "facts.json", strict=True).refresh_digests(store)
    print(f"pm-store: re-recorded {count} digests over the signed bytes", flush=True)


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in ("stage", "reseal"):
        raise SystemExit(__doc__)
    bundle = Path(sys.argv[2]).resolve()
    if sys.argv[1] == "stage":
        if len(sys.argv) != 4:
            raise SystemExit(__doc__)
        stage(bundle, sys.argv[3])
    else:
        reseal(bundle)
    return 0


if __name__ == "__main__":
    sys.exit(main())
