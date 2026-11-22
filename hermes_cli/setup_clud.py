"""``hermes setup clud``: let clud own this install's provider configuration.

clud already installs this product (``clud install hermes``) and, in the same
command, offers to migrate its stored provider credentials into the product's
``config.yaml``. This section delegates to that one implementation instead of
reading ``~/.clud`` or writing a config file of another agent's format here:
one door, no drift between the two directions.

``--print`` shows the command and changes nothing. Without ``clud`` on ``PATH``
the section prints the install page and exits 2 rather than guessing.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from hermes_constants import get_hermes_home
from hermes_platform.resolver import locate_command

from hermes_cli.cli_output import print_error, print_info, print_success

#: The product name clud knows this install by (``clud install <product>``).
CLUD_PRODUCT = "hermes"

#: The command name this section is run as, for its own hints.
CLI_NAME = "hermes"

#: Where clud's own installer lives, for a machine that has none.
CLUD_INSTALL_PAGE = "https://talos-hq.github.io/clud/install/index.html"

#: Exit status when clud is missing: a refused request, not a crash.
EXIT_NOCLUD = 2
CLUD_CODEX_LABEL = "Codex subscription via clud"
CLUD_CODEX_PROVIDER = "clud-codex"


def install_dir() -> Path:
    """The directory this installation lives in, which clud configures."""
    return Path(__file__).parent.parent.resolve()


def clud_install_command(clud: str = "clud", target: Path | None = None, *, noninteractive: bool = False) -> list[str]:
    """The one command this section runs.

    ``--configure-only`` because the product is already installed; ``--target``
    so clud edits *this* installation's config rather than the default one.
    """
    command = [
        clud,
        "install",
        CLUD_PRODUCT,
        "--configure-only",
        "--target",
        str(target if target is not None else install_dir()),
    ]
    if noninteractive:
        command.extend(["--yes", "--migrate-credentials"])
    return command


def clud_available() -> bool:
    """Only advertise the integration when clud is on this backend's PATH."""
    return locate_command("clud").found


def _run_clud_install(command: list[str], *, noninteractive: bool = False):
    # A multiplexed backend's ambient environment belongs to its launch profile.
    env = {**os.environ, "HERMES_HOME": str(get_hermes_home())}
    options = {"env": env}
    if noninteractive:
        options.update(stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    return subprocess.run(command, check=False, **options)


def configure_clud_codex() -> dict:
    """Configure with clud, require a live catalog, and warm the normal picker cache.

    Never return subprocess output: migration output may contain credentials.
    A failed probe must not accept the picker's stale-cache fallback.
    """
    from hermes_cli.config import _CONFIG_LOCK, load_config
    from hermes_cli.models import cached_fetch_api_models, fetch_api_models

    command = locate_command("clud").command
    if not command:
        raise RuntimeError(f"Install clud: {CLUD_INSTALL_PAGE}")
    try:
        # Serialize the child writer with Hermes config writers, without the skills lock.
        # Release before catalog I/O so another profile can configure concurrently.
        with _CONFIG_LOCK:
            completed = _run_clud_install(clud_install_command(command[0], noninteractive=True), noninteractive=True)
            entry = (load_config().get("providers") or {}).get(CLUD_CODEX_PROVIDER) or {}
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Could not finish clud setup. Retry hermes setup clud.") from exc
    if completed.returncode:
        raise RuntimeError(f"clud setup exited {completed.returncode}. Run hermes setup clud to retry.")
    base_url = entry.get("base_url")
    api_key = entry.get("api_key")
    api_mode = entry.get("api_mode", "chat_completions")
    if not base_url or not api_key:
        raise RuntimeError("clud did not configure clud-codex. Run clud serve --codex, then retry setup.")
    models = fetch_api_models(api_key, base_url, api_mode=api_mode)
    if not models:
        raise RuntimeError("Could not discover clud-codex models. Run clud serve --codex, then retry setup.")
    cached_fetch_api_models(api_key, base_url, api_mode=api_mode, force_refresh=True, fetch_models=lambda: models)
    return {"provider": CLUD_CODEX_PROVIDER, "models": models}


def select_clud_model(current_model: str) -> None:
    """CLI renderer of the same setup helper used by Desktop onboarding."""
    from hermes_cli.config import load_config
    from hermes_cli.model_setup_flows_custom import _pick_named_custom_model
    from hermes_cli.model_switch import persist_model_selection, switch_model

    try:
        result = configure_clud_codex()
    except RuntimeError as exc:
        print_error(str(exc))
        return
    model = _pick_named_custom_model(CLUD_CODEX_LABEL, result["models"], current_model)
    if model is None:
        return
    selection = switch_model(model, "", current_model, explicit_provider=CLUD_CODEX_PROVIDER,
                             user_providers=load_config().get("providers"), is_global=True)
    if not selection.success:
        print_error(selection.error_message)
        return
    persist_model_selection(selection)
    print_success(f"Model set to: {model} ({CLUD_CODEX_LABEL})")


def print_clud_command(stream=None) -> None:
    """``hermes setup clud --print``: show the command, change nothing."""
    out = stream if stream is not None else sys.stdout
    print("  clud would run:", file=out)
    print(f"    {shlex.join(clud_install_command())}", file=out)
    print(
        "  It offers to migrate clud's stored provider credentials into this "
        "installation's config.yaml.",
        file=out,
    )


def run_clud_section() -> int:
    """Run ``clud install <product> --configure-only`` for this installation."""
    command = locate_command("clud").command
    if not command:
        print_error("clud is not on PATH.")
        print_info("Install clud first: " + CLUD_INSTALL_PAGE)
        print_info(f"Then re-run: {CLI_NAME} setup clud")
        return EXIT_NOCLUD
    command = clud_install_command(command[0])
    print_info("Handing provider setup to clud:")
    print_info(f"  {shlex.join(command)}")
    print_info(
        "clud owns the credential store and the migration prompt; nothing is "
        "written here directly."
    )
    completed = _run_clud_install(command)
    if completed.returncode != 0:
        print_error(f"clud exited {completed.returncode}; config left unchanged.")
        return completed.returncode
    print_success("clud finished; this installation is configured.")
    return 0
