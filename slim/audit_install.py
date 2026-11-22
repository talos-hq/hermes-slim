#!/usr/bin/env python3
"""Install hermes-slim for real on this Windows or macOS machine and audit the result.

    python slim/audit_install.py [--version latest|vX.Y.Z] [--work DIR] [--report FILE]
                                 [--public-installer]

Three properties (meta issue #138):

1. signed    every Windows PE (.exe/.dll/.pyd) has a valid Authenticode signature;
             every Mach-O carries a Developer ID signature that `codesign` accepts.
2. bundled   the release ships one .tar.zst per target with Python, Node, npm and ripgrep
             (and git on Windows; POSIX uses the system git by PM's design) inside,
             Hermes' PM resolves each of them in the bundle, and installing (plus a
             first `hermes --version`) never runs a package manager.
3. parallel  the bundle arrives as concurrent HTTP range requests, and independent
             downloads (bundle, .sha256, zstd helper) overlap instead of queueing.

Two installs run with the checkout's own installer: one from the GitHub release
(what users get) and one with --archive against slim/tests/range_server.py serving the
same files, whose timing log shows which requests overlapped. --public-installer adds a
third run through the https://hermes-slim.com one-liner.

Writes a Markdown report (the job summary in CI) and exits 1 when any check fails.
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

SLIM = Path(__file__).resolve().parent
sys.path.insert(0, str(SLIM))
import macho_signatures

REPO = "talos-hq/hermes-slim"
WINDOWS = sys.platform == "win32"
MACOS = sys.platform == "darwin"
# Package managers an install must never start. Each is shadowed by a shim that logs
# the call and fails, so a hidden dependency on one shows up instead of silently working.
PACKAGE_MANAGERS = ("pip", "pip3", "uv", "uvx", "npm", "npx", "pnpm", "yarn", "brew", "port",
                    "winget", "choco", "scoop")
# Runtimes Hermes' pm would otherwise download on first use (pm/lock.json).
# PM package -> label. Resolved through Hermes' own PM (pm.installed_package) with the
# bundle's interpreter: the file existing is not enough, PM must find it there.
REQUIRED_RUNTIMES = {"node": "Node.js", "npm": "npm", "ripgrep": "ripgrep"}
WINDOWS_RUNTIMES = {"git": "git"}
RESOLVE = """
import json, sys, pm
out = {}
for name in sys.argv[1:]:
    found = pm.installed_package(name)
    out[name] = str(found.binary) if found and found.binary else None
print(json.dumps(out))
"""
OPTIONAL_RUNTIMES = {"ffmpeg": "ffmpeg", "chromium": "Chromium"}


@dataclass
class Report:
    rows: list[tuple[str, str, str, str]] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def add(self, area: str, check: str, ok: bool | None, detail: str) -> None:
        status = "PASS" if ok else ("INFO" if ok is None else "FAIL")
        self.rows.append((area, check, status, detail))
        print(f"[{status}] {area}: {check} — {detail}", flush=True)

    @property
    def failed(self) -> bool:
        return any(r[2] == "FAIL" for r in self.rows)

    def markdown(self, title: str) -> str:
        out = [f"## {title}", "", "| Area | Check | Result | Detail |", "|---|---|---|---|"]
        icon = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "INFO": "ℹ️ INFO"}
        for area, check, status, detail in self.rows:
            out.append(f"| {area} | {check} | {icon[status]} | {detail.replace('|', '/')} |")
        for block in self.details:
            out += ["", block]
        return "\n".join(out) + "\n"


def machine_target() -> tuple[str, str]:
    """(the target this machine should run natively, the target the installer picks)."""
    arm = platform.machine().lower() in ("arm64", "aarch64") or os.environ.get("PROCESSOR_ARCHITEW6432", "").upper() == "ARM64" \
        or os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "ARM64"
    if WINDOWS:
        # The installer falls back to win32-x64 when a release has no native bundle; main()
        # switches to the native one when it is published.
        return ("win32-arm64" if arm else "win32-x64"), "win32-x64"
    if MACOS:
        # Under Rosetta platform.machine() says x86_64; sysctl tells the truth.
        native_arm = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True, timeout=30, check=False).stdout.strip() == "1"
        target = "darwin-arm64" if native_arm else "darwin-x64"
        return target, target
    sys.exit("audit_install.py audits Windows and macOS installs")


def api(path: str) -> dict:
    req = urllib.request.Request(f"https://api.github.com/{path}", headers={"Accept": "application/vnd.github+json"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=600) as resp, dest.open("wb") as f:
        shutil.copyfileobj(resp, f, 1 << 20)


def make_shims(root: Path, log: Path) -> Path:
    shims = root / "shims"
    shims.mkdir(parents=True, exist_ok=True)
    for name in PACKAGE_MANAGERS:
        if WINDOWS:
            (shims / f"{name}.cmd").write_text(f'@echo {name} %*>>"{log}"\r\n@exit /b 127\r\n')
        else:
            shim = shims / name
            shim.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\nexit 127\n')
            shim.chmod(0o755)
    return shims


def run(cmd: list[str], env: dict, log: Path, timeout: int = 1800) -> tuple[int, float]:
    started = time.monotonic()
    with log.open("w") as out:
        proc = subprocess.run(cmd, env=env, stdout=out, stderr=subprocess.STDOUT, timeout=timeout, check=False)
    return proc.returncode, time.monotonic() - started


def installer_cmd(target_dir: Path, *, version: str | None = None, archive: str | None = None) -> list[str]:
    if WINDOWS:
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SLIM / "install.ps1"),
               "-Dir", str(target_dir), "-NoSetup"]
        if archive:
            cmd += ["-Archive", archive]
        elif version and version != "latest":
            cmd += ["-Version", version]
        return cmd
    cmd = ["sh", str(SLIM / "install.sh"), "--dir", str(target_dir), "--no-setup"]
    if archive:
        cmd += ["--archive", archive]
    elif version and version != "latest":
        cmd += ["--version", version]
    return cmd


def without_zstd(env: dict) -> dict:
    """PATH minus directories holding a zstd (Homebrew's on CI Macs), so the installer
    fetches its standalone helper the way it must on a stock Mac."""
    if WINDOWS:
        return env
    keep = [d for d in env["PATH"].split(os.pathsep) if not (Path(d) / "zstd").exists()]
    return {**env, "PATH": os.pathsep.join(keep)}


def overlap(intervals: list[tuple[float, float]]) -> int:
    """Most intervals open at one instant."""
    events = sorted([(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals], key=lambda x: (x[0], x[1]))
    best = cur = 0
    for _, step in events:
        cur += step
        best = max(best, cur)
    return best


def check_downloads(report: Report, timing: Path, asset: str) -> None:
    rows = []
    for line in timing.read_text().splitlines() if timing.exists() else []:
        start, end, method, path, *rng = line.split(" ", 4)
        rows.append((float(start), float(end), method, path.lstrip("/"), rng[0] if rng else ""))
    gets = [r for r in rows if r[2] == "GET"]
    bundle = [r for r in gets if r[3] == asset and r[4] and r[4] != "bytes=0-0"]
    report.add("parallel", "bundle fetched as HTTP range requests", len(bundle) >= 2,
               f"{len(bundle)} ranged GETs of {asset}")
    peak = overlap([(r[0], r[1]) for r in bundle])
    report.add("parallel", "bundle ranges in flight at once", peak >= 2, f"peak {peak} concurrent")
    # The bundle's own .sha256 decides whether the bundle is needed at all, so it may come
    # first; anything else (the zstd helper and its checksum) has no reason to wait. A small
    # file can finish before the first range starts, so "not queued" means it started before
    # the bundle's last range ended.
    others = [r for r in gets if r[3] not in (asset, f"{asset}.sha256")]
    bundle_done = max((b[1] for b in bundle), default=0.0)
    queued = [r for r in others if r[0] >= bundle_done]
    report.add("parallel", "independent downloads don't wait for the bundle", not queued,
               f"{len(others) - len(queued)}/{len(others)} of {sorted({r[3] for r in others})} started before the bundle finished"
               if others else "no independent downloads on this platform")
    report.details.append("<details><summary>request timeline (range server)</summary>\n\n```\n"
                          + "\n".join(f"{r[0] - rows[0][0]:8.3f}s → {r[1] - rows[0][0]:8.3f}s  {r[2]} {r[3]} {r[4]}" for r in rows)
                          + "\n```\n</details>" if rows else "")


def check_signatures(report: Report, tree: Path, label: str) -> None:
    if WINDOWS:
        ps = ("$ErrorActionPreference='Stop'; Get-ChildItem -LiteralPath $env:AUDIT_TREE -Recurse -File "
              "| Where-Object { $_.Extension -in '.exe','.dll','.pyd' } | Get-AuthenticodeSignature | ForEach-Object { [pscustomobject]@{ Path = $_.Path; Status = [string]$_.Status; "
              "Signer = if ($_.SignerCertificate) { $_.SignerCertificate.Subject } else { '' } } } | ConvertTo-Json -Compress")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], env={**os.environ, "AUDIT_TREE": str(tree)},
                             capture_output=True, text=True, timeout=1200, check=True).stdout.strip() or "[]"
        items = json.loads(out)
        items = items if isinstance(items, list) else [items]
        bad = [i for i in items if i["Status"] != "Valid"]
        signers = sorted({re.sub(r",.*", "", i["Signer"]) for i in items if i["Signer"]})
        report.add("signed", f"{label}: Authenticode Valid on every PE", bool(items) and not bad,
                   f"{len(items) - len(bad)}/{len(items)} valid; signers: {', '.join(signers) or '-'}")
        if bad:
            report.details.append(f"<details><summary>{label}: {len(bad)} PE files not Valid</summary>\n\n```\n"
                                  + "\n".join(f"{i['Status']:>14}  {Path(i['Path']).relative_to(tree)}" for i in bad[:300])
                                  + "\n```\n</details>")
        return
    found = macho_signatures.scan(tree)
    weak: list[str] = []
    for path, kind in found.items():
        info = subprocess.run(["codesign", "-dv", "--verbose=2", str(path)], capture_output=True, text=True, timeout=60, check=False).stderr
        verify = subprocess.run(["codesign", "--verify", "--strict", str(path)], capture_output=True, text=True, timeout=60, check=False)
        authority = re.search(r"^Authority=(.*)$", info, re.MULTILINE)
        if not (authority and authority.group(1).startswith("Developer ID Application") and verify.returncode == 0):
            why = "unsigned" if "not signed" in info else ("adhoc" if "Signature=adhoc" in info else
                                                           (authority.group(1) if authority else kind))
            if verify.returncode:
                why += f"; verify: {verify.stderr.strip().splitlines()[-1] if verify.stderr.strip() else 'failed'}"
            weak.append(f"{why:>12}  {path.relative_to(tree)}")
    report.add("signed", f"{label}: Developer ID signature on every Mach-O", bool(found) and not weak,
               f"{len(found) - len(weak)}/{len(found)} Developer ID")
    if weak:
        report.details.append(f"<details><summary>{label}: {len(weak)} Mach-O files without a valid Developer ID signature</summary>\n\n```\n"
                              + "\n".join(weak[:300]) + "\n```\n</details>")


def find_runtime(tree: Path, name: str) -> Path | None:
    names = {f"{name}.exe", f"{name}.cmd", name} if WINDOWS else {name}
    for path in tree.rglob("*"):
        if path.name in names and path.is_file() and (WINDOWS or os.access(path, os.X_OK)):
            return path
    return None


def pm_resolution(tree: Path, python: Path, env: dict, names: list[str]) -> dict[str, str | None]:
    """Where Hermes' PM finds each package, asked with the bundle's own interpreter."""
    # -P: `-c` would put the working directory (a checkout, in CI) ahead of the bundle's app/.
    proc = subprocess.run([str(python), "-P", "-c", RESOLVE, *names], capture_output=True, text=True, timeout=300,
                          check=False, env={**env, "PYTHONPATH": str(tree / "app")})
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        print(proc.stdout, proc.stderr, sep="\n")
        return {}


def check_contents(report: Report, tree: Path, asset: str, shim_log: Path, env: dict, work: Path) -> None:
    report.add("bundled", "release bundle is one .tar.zst", asset.endswith(".tar.zst"), asset)
    python = tree / "python" / ("python.exe" if WINDOWS else "bin/python3")
    if python.exists():
        ver = subprocess.run([str(python), "-c", "import sys; print(sys.version.split()[0])"], capture_output=True, text=True, timeout=120, check=False)
        report.add("bundled", "Python ships in the bundle", ver.returncode == 0, f"{python.relative_to(tree)} {ver.stdout.strip()}")
    else:
        report.add("bundled", "Python ships in the bundle", False, f"no {python.relative_to(tree)}")
    required = {**REQUIRED_RUNTIMES, **(WINDOWS_RUNTIMES if WINDOWS else {})}
    resolved = pm_resolution(tree, python, env, list(required))
    for name, label in required.items():
        hit = resolved.get(name)
        inside = hit is not None and Path(hit).resolve().is_relative_to(tree.resolve())
        if inside:
            ver = subprocess.run([hit, "--version"], capture_output=True, text=True, timeout=120, check=False)
            detail = f"{Path(hit).relative_to(tree)}: {(ver.stdout or ver.stderr).strip().splitlines()[0] if ver.returncode == 0 else f'exit {ver.returncode}'}"
            ok = ver.returncode == 0
        else:
            detail, ok = (f"PM resolves {hit}, outside the bundle" if hit else
                          "not in the bundle; Hermes' PM downloads it on first use"), False
        report.add("bundled", f"{label} ships in the bundle", ok, detail)
    if not WINDOWS:
        report.add("bundled", "git ships in the bundle", None, "system git on POSIX by PM's design (pm/packages.py Git)")
    for exe, label in OPTIONAL_RUNTIMES.items():
        hit = find_runtime(tree, exe)
        report.add("bundled", f"{label} ships in the bundle (optional)", None,
                   str(hit.relative_to(tree)) if hit else "not in bundle (pm downloads it when a feature needs it)")
    hermes = tree / "bin" / ("hermes.cmd" if WINDOWS else "hermes")
    code, secs = run([str(hermes), "--version"], env, work / "first-launch.log", timeout=300)
    first = (work / "first-launch.log").read_text(errors="replace").strip().splitlines()
    report.add("bundled", "first `hermes --version` runs", code == 0, f"exit {code} in {secs:.1f}s: {first[-1] if first else ''}")
    calls = shim_log.read_text().strip().splitlines() if shim_log.exists() else []
    report.add("bundled", "no package manager ran during install or first launch", not calls,
               "; ".join(calls[:8]) if calls else "none")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", default="latest")
    ap.add_argument("--work", type=Path, default=Path.cwd() / "audit-work")
    ap.add_argument("--report", type=Path)
    ap.add_argument("--public-installer", action="store_true")
    args = ap.parse_args()
    # Windows consoles default to cp1252; the report has ✅/❌.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    native, target = machine_target()
    report = Report()

    rel = api(f"repos/{REPO}/releases/latest" if args.version == "latest" else f"repos/{REPO}/releases/tags/{args.version}")
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    published = any(f"-{native}." in n for n in assets)
    if WINDOWS and published:
        target = native  # install.ps1 picks the native bundle when the release has one (#143)
    report.add("bundled", f"native {native} bundle published", published,
               "found" if published else f"no {native} asset in {rel['tag_name']}; the installer uses {target}")
    # Prefer the .tar.zst: a Windows release carries a transitional .zip as well (#141).
    asset = next((n for ext in ("tar.zst", "zip") for n in assets if n.endswith(f"-{target}.{ext}")), None)
    if not asset:
        report.add("bundled", f"{target} bundle published", False, f"none in {rel['tag_name']}")
        return finish(report, args, rel["tag_name"], native, target)

    shim_log = work / "package-managers.log"
    shims = make_shims(work, shim_log)
    # HERMES_HOME keeps the first launch off the runner's real profile.
    env = {**os.environ, "PATH": f"{shims}{os.pathsep}{os.environ['PATH']}", "HERMES_HOME": str(work / "hermes-home")}

    # 1. The release install users get.
    code, secs = run(installer_cmd(work / "release-install", version=rel["tag_name"]), env, work / "install-release.log")
    tail = (work / "install-release.log").read_text(errors="replace").strip().splitlines()[-3:]
    report.add("install", f"installer from GitHub release {rel['tag_name']}", code == 0, f"exit {code} in {secs:.0f}s; " + " / ".join(tail))
    tree = work / "release-install" / "current"
    if code == 0:
        check_signatures(report, tree, "installed tree")
        check_contents(report, tree, asset, shim_log, env, work)
    # The parallel unpacker (slim/unpack): when the release publishes one for this target, the
    # installer must have unpacked with it rather than zstd | tar.
    unpacker = next((n for n in assets if n.startswith(f"hermes-slim-unpack-{target}") and not n.endswith(".sha256")), None)
    log = (work / "install-release.log").read_text(errors="replace")
    used = next((line.strip() for line in log.splitlines() if line.startswith("unpacked ")), "")
    report.add("unpack", "bundle unpacked by hermes-slim-unpack (parallel decode + writes)",
               bool(used) if unpacker else None, used or (f"{unpacker} published but not used" if unpacker
                                                         else f"no hermes-slim-unpack-{target} in {rel['tag_name']}"))

    # 2. The same files through the range server, to see what ran in parallel.
    mirror = work / "mirror"
    # Both Windows targets unpack with the x64 helper (no ARM64 zstd build upstream).
    helper = "zstd-win32-x64" if WINDOWS else f"zstd-{target}"
    for name in [asset, f"{asset}.sha256"] + [n for n in assets if n.startswith((helper, f"hermes-slim-unpack-{target}"))]:
        download(assets[name], mirror / name)
    # The helper runs on the user's machine before anything in the bundle: it must be signed too.
    if MACOS or WINDOWS:
        prefixes = ("zstd-darwin", "hermes-slim-unpack-darwin") if MACOS else (helper, "hermes-slim-unpack-win32")
        helpers = [n for n in assets if n.startswith(prefixes) and not n.endswith(".sha256")]
        for name in helpers:
            download(assets[name], work / "helpers" / name)
        if helpers:
            check_signatures(report, work / "helpers", "zstd and unpack helpers")
    server = subprocess.Popen([sys.executable, str(SLIM / "tests" / "range_server.py"), str(mirror)],
                              stdout=subprocess.PIPE, text=True)
    try:
        port = int(server.stdout.readline())
        code, secs = run(installer_cmd(work / "mirror-install", archive=f"http://127.0.0.1:{port}/{asset}"),
                         without_zstd(env), work / "install-mirror.log")
        tail = (work / "install-mirror.log").read_text(errors="replace").strip().splitlines()[-2:]
        report.add("install", "installer against the range server", code == 0, f"exit {code} in {secs:.0f}s; " + " / ".join(tail))
    finally:
        server.terminate()
    check_downloads(report, mirror / "timing.log", asset)

    if args.public_installer:
        pub = work / "public-install"
        if WINDOWS:
            cmd = ["powershell", "-NoProfile", "-Command",
                   f"& ([scriptblock]::Create((irm https://hermes-slim.com/windows/install.ps1))) -Dir '{pub}' -NoSetup"]
        else:
            sub = "macos" if MACOS else "linux"
            cmd = ["sh", "-c", f"curl -fsSL https://hermes-slim.com/{sub}/install.sh | sh -s -- --dir '{pub}' --no-setup"]
        code, secs = run(cmd, env, work / "install-public.log")
        tail = (work / "install-public.log").read_text(errors="replace").strip().splitlines()[-2:]
        report.add("install", "public one-liner from hermes-slim.com", code == 0, f"exit {code} in {secs:.0f}s; " + " / ".join(tail))

    return finish(report, args, rel["tag_name"], native, target)


def finish(report: Report, args, tag: str, native: str, target: str) -> int:
    title = f"hermes-slim install audit — {tag} on {platform.system()} {platform.machine()} (native {native}, installs {target})"
    md = report.markdown(title)
    if args.report:
        with args.report.open("a", encoding="utf-8") as f:
            f.write(md)
    print(md)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
