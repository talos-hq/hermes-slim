"""What a bundle build needs to know about a target it cannot run.

A cross build (slim/build_bundle.py on Linux for any target) never executes the target's
interpreter, so the facts that interpreter would report live here: its PEP 508 marker
environment, uv's platform name for it, and where its stdlib and site-packages sit.

uv's --python-platform is not enough on its own: for x86_64-pc-windows-msvc uv evaluates
platform_machine as "x86_64", while CPython on Windows reports "AMD64", and every
requirement gated on `platform_machine == 'AMD64'` would silently drop out of the bundle.
So the build evaluates the exported markers itself (select_requirements) and hands uv a
marker-free list.

Stdlib only.
"""

from __future__ import annotations

import re
from pathlib import Path

SLIM = Path(__file__).resolve().parent

# uv --python-platform per target. The glibc ones pin the oldest glibc the bundle supports.
UV_PLATFORM = {
    "darwin-x64": "x86_64-apple-darwin",
    "darwin-arm64": "aarch64-apple-darwin",
    "win32-x64": "x86_64-pc-windows-msvc",
    "linux-x64": "x86_64-manylinux_2_28",
    "linux-arm64": "aarch64-manylinux_2_28",
    "linux-x64-musl": "x86_64-unknown-linux-musl",
}

# What sys.platform, platform.system(), platform.machine() and os.name report on each target.
_PLATFORM = {
    "darwin-x64": ("darwin", "Darwin", "x86_64", "posix"),
    "darwin-arm64": ("darwin", "Darwin", "arm64", "posix"),
    "win32-x64": ("win32", "Windows", "AMD64", "nt"),
    "linux-x64": ("linux", "Linux", "x86_64", "posix"),
    "linux-arm64": ("linux", "Linux", "aarch64", "posix"),
    "linux-x64-musl": ("linux", "Linux", "x86_64", "posix"),
}


def marker_env(target: str, python_version: str) -> dict[str, str]:
    """PEP 508 marker environment of the bundled CPython on target (python_version: X.Y.Z)."""
    sys_platform, system, machine, os_name = _PLATFORM[target]
    return {
        "sys_platform": sys_platform,
        "platform_system": system,
        "platform_machine": machine,
        "os_name": os_name,
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
        "implementation_version": python_version,
        "python_version": ".".join(python_version.split(".")[:2]),
        "python_full_version": python_version,
        # Kernel facts no target pins; the lock only tests them for 'android'.
        "platform_release": "",
        "platform_version": "",
        "extra": "",
    }


_TOKEN = re.compile(r"""\s*(?:(?P<str>'[^']*'|"[^"]*")|(?P<op>===|==|!=|<=|>=|~=|<|>|\(|\))|(?P<word>[A-Za-z_][A-Za-z0-9_.]*))""")
_VERSION_VARS = {"python_version", "python_full_version", "implementation_version"}


def _tokens(marker: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(marker.rstrip()):
        m = _TOKEN.match(marker, pos)
        if not m:
            raise ValueError(f"bad marker at {marker[pos:]!r}: {marker!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(re.match(r"\d*", part).group() or 0) for part in text.split("."))


def _compare_versions(left: str, op: str, right: str) -> bool:
    if right.endswith(".*") and op in ("==", "!="):
        prefix = _version(right[:-2])
        return (_version(left)[:len(prefix)] == prefix) == (op == "==")
    a, b = _version(left), _version(right)
    width = max(len(a), len(b))
    a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
    if op == "~=":
        floor = _version(right)
        return a >= b and _version(left)[:len(floor) - 1] == floor[:-1]
    return {"==": a == b, "===": left == right, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def evaluate_marker(marker: str, env: dict[str, str]) -> bool:
    """Evaluate a PEP 508 environment marker (the subset `uv export` writes) against env."""
    toks = _tokens(marker)
    pos = 0

    def peek() -> tuple[str, str] | None:
        return toks[pos] if pos < len(toks) else None

    def take() -> tuple[str, str]:
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def value() -> tuple[str, bool]:
        kind, text = take()
        if kind == "str":
            return text[1:-1], False
        if text not in env:
            raise ValueError(f"unknown marker variable {text!r} in {marker!r}")
        return env[text], text in _VERSION_VARS

    def comparison() -> bool:
        if peek() == ("op", "("):
            take()
            result = disjunction()
            if take() != ("op", ")"):
                raise ValueError(f"unbalanced parentheses in {marker!r}")
            return result
        left, left_version = value()
        kind, op = take()
        if (kind, op) == ("word", "not"):
            if take() != ("word", "in"):
                raise ValueError(f"expected 'not in' in {marker!r}")
            op = "not in"
        elif kind == "word" and op != "in":
            raise ValueError(f"bad operator {op!r} in {marker!r}")
        right, right_version = value()
        if op == "in":
            return left in right
        if op == "not in":
            return left not in right
        if left_version or right_version:
            return _compare_versions(left, op, right)
        if op in ("==", "==="):
            return left == right
        if op == "!=":
            return left != right
        return _compare_versions(left, op, right)

    def conjunction() -> bool:
        result = comparison()
        while peek() == ("word", "and"):
            take()
            result = comparison() and result
        return result

    def disjunction() -> bool:
        result = conjunction()
        while peek() == ("word", "or"):
            take()
            result = conjunction() or result
        return result

    result = disjunction()
    if pos != len(toks):
        raise ValueError(f"trailing tokens in marker {marker!r}")
    return result


def select_requirements(text: str, env: dict[str, str]) -> list[str]:
    """The `name==version` lines of a `uv export` requirements file that apply under env."""
    keep = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement, _, marker = line.partition(";")
        if not marker.strip() or evaluate_marker(marker.strip(), env):
            keep.append(requirement.strip())
    return keep


def stdlib_dir(target: str, python_version: str) -> str:
    """The bundle-relative stdlib directory of target's CPython."""
    if target.startswith("win32"):
        return "python/Lib"
    return "python/lib/python" + ".".join(python_version.split(".")[:2])


def site_packages(target: str, python_version: str) -> str:
    return stdlib_dir(target, python_version) + "/site-packages"


def neutral(rel: str, target: str, python_version: str) -> str:
    """A bundle-relative path with the target's stdlib and site-packages prefixes made generic."""
    for prefix, alias in ((site_packages(target, python_version), "SP"), (stdlib_dir(target, python_version), "STD")):
        if rel.startswith(prefix + "/"):
            return alias + rel[len(prefix):]
    return rel


def localize(key: str, target: str, python_version: str) -> str:
    """The inverse of neutral()."""
    head, _, rest = key.partition("/")
    if head == "SP":
        return site_packages(target, python_version) + "/" + rest
    if head == "STD":
        return stdlib_dir(target, python_version) + "/" + rest
    return key


def warm_extras(target: str) -> list[str]:
    """Modules target's first launch imports beyond a Linux one (slim/warm/<target>.txt)."""
    path = SLIM / "warm" / f"{target}.txt"
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.startswith("#")]


def warm_modules(bundle: Path, target: str, python_version: str) -> set[str]:
    """Neutral paths of the modules bundle carries bytecode for."""
    out = set()
    for pyc in bundle.rglob("*.pyc"):
        src = pyc.parent.parent / (pyc.name.split(".", 1)[0] + ".py")
        out.add(neutral(src.relative_to(bundle).as_posix(), target, python_version))
    return out


def main() -> None:
    """Regenerate slim/warm/<target>.txt from two natively built bundles.

        python slim/cross.py warm-extras LINUX_X64_BUNDLE TARGET_BUNDLE TARGET
    """
    import argparse

    ap = argparse.ArgumentParser(description=main.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["warm-extras"])
    ap.add_argument("linux_bundle", type=Path, help="a native linux-x64 hermes-slim/ tree")
    ap.add_argument("target_bundle", type=Path, help="a native hermes-slim/ tree for TARGET")
    ap.add_argument("target", choices=sorted(UV_PLATFORM))
    ap.add_argument("--python-version", default="3.14.9")
    args = ap.parse_args()
    base = warm_modules(args.linux_bundle, "linux-x64", args.python_version)
    extra = sorted(warm_modules(args.target_bundle, args.target, args.python_version) - base)
    out = SLIM / "warm" / f"{args.target}.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text(
        f"# Modules a {args.target} first launch imports that a linux-x64 one does not. A cross\n"
        "# build precompiles these on top of its Linux warm-up. Regenerate from native bundles:\n"
        f"#   python slim/cross.py warm-extras LINUX_X64_BUNDLE TARGET_BUNDLE {args.target}\n"
        + "".join(f"{key}\n" for key in extra))
    print(f"{out}: {len(extra)} modules")


if __name__ == "__main__":
    main()
