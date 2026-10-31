#!/usr/bin/env python3
"""hermes-slim setup: configure Hermes for OpenRouter and bring the gateway up.

Interactive by default. Every value also has a flag and an environment
variable, so CI (and scripted installs) can run with --non-interactive:

    hermes-slim-setup --non-interactive --first-name "Ada" --last-name "Lovelace" \\
        --openrouter-key sk-or-... --skip-online-checks

Writes the same config.yaml / .env keys Hermes' own wizard writes when the user
picks OpenRouter, using Hermes' own writers, then starts the gateway and waits
for its /health endpoint.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_API_PORT = 8642
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]*$")


class SetupError(Exception):
    pass


# ---------------------------------------------------------------- validation

def clean_name(raw: str, field: str) -> str:
    """Trim, collapse inner whitespace, and validate a first/last name.

    Inner spaces are removed ("Mary Ann" -> "MaryAnn") because the name is
    later used in Telegram bot usernames, which allow no spaces.
    """
    name = "".join((raw or "").split())
    if not name:
        raise SetupError(f"{field} is required")
    if len(name) > 32:
        raise SetupError(f"{field} must be at most 32 characters")
    if not _NAME_RE.match(name):
        raise SetupError(f"{field} may contain only letters, apostrophes and hyphens: {raw!r}")
    return name


def username_part(name: str) -> str:
    """Lowercase ASCII form of a name for bot usernames (apostrophes/hyphens dropped)."""
    return re.sub(r"[^a-z]", "", name.lower())


def bot_username(first: str, last: str, n: int) -> str:
    """hermes_<first>_<last>_<N>_bot, within Telegram's 5-32 character limit."""
    username = f"hermes_{username_part(first)}_{username_part(last)}_{n}_bot"
    if len(username) > 32:
        raise SetupError(f"bot username {username!r} exceeds Telegram's 32-character limit")
    return username


def clean_openrouter_key(raw: str) -> str:
    key = (raw or "").strip()
    if not key:
        raise SetupError("OpenRouter API key is required")
    if not key.startswith("sk-or-") or len(key) < 20 or any(c.isspace() for c in key):
        raise SetupError("OpenRouter API key should look like sk-or-v1-...")
    return key


def check_openrouter_key(key: str) -> None:
    req = urllib.request.Request(f"{OPENROUTER_BASE_URL}/key", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            json.load(r)
    except urllib.error.HTTPError as e:
        raise SetupError(f"OpenRouter rejected the key (HTTP {e.code})") from e
    except OSError as e:
        raise SetupError(f"could not reach OpenRouter to check the key: {e}") from e


# ---------------------------------------------------------------- prompting

def ask(label: str, value: str | None, clean, *, secret: bool = False, interactive: bool, default: str | None = None):
    """Return clean(value), prompting (and re-prompting on bad input) when interactive."""
    if value:
        return clean(value)
    if not interactive:
        if default is not None:
            return clean(default)
        raise SetupError(f"{label} is required (pass it as a flag or environment variable)")
    while True:
        suffix = f" [{default}]" if default else ""
        raw = getpass.getpass(f"{label}: ") if secret else input(f"{label}{suffix}: ")
        try:
            return clean(raw or default or "")
        except SetupError as e:
            print(f"  ✗ {e}")


# ---------------------------------------------------------------- config

def write_config(first: str, last: str, key: str, model: str, api_port: int) -> dict:
    """Autofill exactly what Hermes' OpenRouter wizard flow writes, plus gateway API settings."""
    from hermes_cli import config as hc

    hc.ensure_hermes_home()
    hc.save_env_value("OPENROUTER_API_KEY", key)
    env = _read_env(hc.get_env_path())
    if len(env.get("API_SERVER_KEY", "")) < 16:
        hc.save_env_value("API_SERVER_KEY", secrets.token_hex(24))
    hc.save_env_value("API_SERVER_HOST", "127.0.0.1")
    hc.save_env_value("API_SERVER_PORT", str(api_port))
    # Hermes loads .env into the environment before provider plugins, so this keeps
    # Nous hidden in processes the launchers don't start (launchd/systemd/task gateway).
    hc.save_env_value("HERMES_SLIM", "1")

    cfg = hc.load_config()
    m = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    m.update(default=model, provider="openrouter", base_url=OPENROUTER_BASE_URL, api_mode="chat_completions")
    m.pop("api_key", None)
    cfg["model"] = m
    nous = cfg.get("nous") if isinstance(cfg.get("nous"), dict) else {}
    nous["guest"] = False
    cfg["nous"] = nous
    cfg["slim"] = {"first_name": first, "last_name": last}
    hc.save_config(cfg)
    return {"home": str(hc.get_config_path().parent), "config": str(hc.get_config_path()), "env": str(hc.get_env_path())}


def _read_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip().removeprefix("export ").strip()] = v.strip().strip("'\"")
    return out


# ---------------------------------------------------------------- gateway

def hermes_cmd(*args: str) -> list[str]:
    return [sys.executable, str(APP / "hermes"), *args]


def health(port: int, key: str | None = None, timeout: float = 3) -> dict | None:
    """/health of the gateway on `port`, or None. With `key`, only if that gateway is ours:
    anything (another Hermes, any web server) can answer /health, only ours takes our key."""
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=timeout) as r:
            data = json.load(r)
        if data.get("status") != "ok":
            return None
        if key:
            req = urllib.request.Request(f"{base}/v1/models", headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=timeout):
                pass
        return data
    except (OSError, ValueError):
        return None


def wait_healthy(port: int, key: str, wait: float) -> dict | None:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if (h := health(port, key)):
            return h
        time.sleep(0.25)
    return None


# How long a just-installed service gets to answer /health before setup runs the
# gateway itself.
SERVICE_GRACE = 30.0


def has_service_manager() -> bool:
    """launchd and the Task Scheduler are always there; on Linux only systemd is supported,
    and Hermes declines services inside containers (even ones running systemd)."""
    if not sys.platform.startswith("linux"):
        return True
    from hermes_platform.host.runtime import is_container

    return Path("/run/systemd/system").is_dir() and not is_container()


def start_gateway(service: bool, port: int, key: str, wait: float, log_path: Path) -> dict:
    env = dict(os.environ, HERMES_NONINTERACTIVE="1", PYTHONPATH=str(APP))
    if service and not has_service_manager():
        print("no systemd user services here (container or no systemd); running the gateway in the background "
              "(it will not start on login)", file=sys.stderr)
        service = False
    if service:
        env.update(HERMES_GATEWAY_INSTALL_START_NOW="1", HERMES_GATEWAY_INSTALL_START_ON_LOGIN="1")
        # With no service manager (containers, WSL without systemd, minimal servers) Hermes
        # declines to install one and still exits 0, so judge the service by /health.
        installed = subprocess.run(hermes_cmd("gateway", "install", "--force", "--start-now", "--start-on-login"),
                                   env=env, stdin=subprocess.DEVNULL).returncode == 0
        if installed and (h := wait_healthy(port, key, SERVICE_GRACE)):
            return h
        print("gateway service did not start; running the gateway in the background instead", file=sys.stderr)
    # Always start our own (--replace takes over this HERMES_HOME's gateway): an answer on
    # the port alone may come from another install, or from something that is not Hermes.
    log = open(log_path, "ab")
    kw = {"creationflags": 0x00000008 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(hermes_cmd("gateway", "run", "--replace"), env=env, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, **kw)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SetupError(f"gateway exited with {proc.returncode} (is port {port} taken? log: {log_path})")
        if (h := health(port, key)):
            return h
        time.sleep(0.25)
    raise SetupError(f"gateway did not report healthy on 127.0.0.1:{port} within {wait:.0f}s "
                     f"(if something else holds that port, pass --api-port; log: {log_path})")


# ---------------------------------------------------------------- main

def parse_args(argv=None) -> argparse.Namespace:
    e = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--first-name", default=e("HS_FIRST_NAME"))
    ap.add_argument("--last-name", default=e("HS_LAST_NAME"))
    ap.add_argument("--openrouter-key", default=e("HS_OPENROUTER_KEY") or e("OPENROUTER_API_KEY"))
    ap.add_argument("--model", default=e("HS_MODEL") or DEFAULT_MODEL)
    ap.add_argument("--api-port", type=int, default=int(e("HS_API_PORT") or DEFAULT_API_PORT))
    ap.add_argument("--non-interactive", action="store_true", default=bool(e("HS_NON_INTERACTIVE")),
                    help="never prompt; fail on missing values")
    ap.add_argument("--skip-online-checks", action="store_true", default=bool(e("HS_SKIP_ONLINE_CHECKS")),
                    help="do not validate the OpenRouter key against the API")
    ap.add_argument("--no-gateway", action="store_true", help="write config only")
    ap.add_argument("--service", action="store_true", default=bool(e("HS_SERVICE")),
                    help="install the gateway as a login service (launchd/systemd/Windows task)")
    ap.add_argument("--gateway-only", action="store_true",
                    help="keep the existing configuration and only (re)start the gateway (upgrades)")
    ap.add_argument("--gateway-wait", type=float, default=180)
    ap.add_argument("--json", action="store_true", help="print a machine-readable summary")
    return ap.parse_args(argv)


def restart_gateway(args) -> dict:
    """Upgrade path: the user configured this HERMES_HOME already; bring its gateway back."""
    from hermes_cli import config as hc

    env = _read_env(hc.get_env_path())
    if not env.get("OPENROUTER_API_KEY"):
        raise SetupError("no existing configuration to keep; run hermes-slim-setup")
    port = int(env.get("API_SERVER_PORT") or args.api_port)
    log = hc.get_config_path().parent / "logs" / "slim-gateway.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "home": str(hc.get_config_path().parent),
            "gateway": start_gateway(args.service, port, env.get("API_SERVER_KEY", ""), args.gateway_wait, log)}


def main(argv=None) -> int:
    args = parse_args(argv)
    interactive = not args.non_interactive and sys.stdin.isatty()
    if args.gateway_only:
        try:
            summary = restart_gateway(args)
        except SetupError as e:
            print(json.dumps({"ok": False, "error": str(e)}) if args.json else f"error: {e}", file=sys.stderr)
            return 2
        print(json.dumps(summary) if args.json else f"✓ gateway healthy ({summary['home']})")
        return 0
    try:
        if interactive:
            print("hermes-slim setup — OpenRouter + DeepSeek\n")
        first = ask("First name", args.first_name, lambda v: clean_name(v, "first name"), interactive=interactive)
        last = ask("Last name", args.last_name, lambda v: clean_name(v, "last name"), interactive=interactive)
        while True:
            key = ask("OpenRouter API key", args.openrouter_key, clean_openrouter_key, secret=True, interactive=interactive)
            if args.skip_online_checks:
                break
            try:
                check_openrouter_key(key)
                break
            except SetupError as e:
                if not interactive:
                    raise
                print(f"  ✗ {e}")
                args.openrouter_key = None
        model = args.model.strip()
        paths = write_config(first, last, key, model, args.api_port)
        summary = {"ok": True, "first_name": first, "last_name": last, "model": model, **paths}
        if not args.no_gateway:
            log = Path(paths["home"]) / "logs" / "slim-gateway.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            api_key = _read_env(Path(summary["env"])).get("API_SERVER_KEY", "")
            summary["gateway"] = start_gateway(args.service, args.api_port, api_key, args.gateway_wait, log)
    except SetupError as e:
        print(json.dumps({"ok": False, "error": str(e)}) if args.json else f"error: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary))
    else:
        print(f"✓ configured {model} via OpenRouter in {summary['home']}")
        if "gateway" in summary:
            print(f"✓ gateway healthy on 127.0.0.1:{args.api_port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
