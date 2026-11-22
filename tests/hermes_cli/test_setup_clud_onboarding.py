"""Codex onboarding delegates configuration to clud in the owning profile."""

import json
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import setup_clud
from hermes_cli.config import load_config
from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override


@pytest.fixture
def fake_clud(tmp_path, monkeypatch):
    executable = tmp_path / "clud"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "home = Path(os.environ['HERMES_HOME'])\n"
        "home.mkdir(parents=True, exist_ok=True)\n"
        "(home / 'invocation.json').write_text(json.dumps({'args': sys.argv[1:], 'home': str(home)}))\n"
        "(home / 'config.yaml').write_text(" + repr('providers:\n  clud-codex:\n    base_url: http://127.0.0.1:8317/v1\n    api_key: fixture-token\n    api_mode: chat_completions\n') + ")\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    return executable


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("secondary", [False, True])
def test_setup_and_route_use_scoped_home_and_refresh_live_models(fake_clud, tmp_path, monkeypatch, secondary):
    from hermes_cli.web_routers import models
    from hermes_cli import models as catalog

    probes = []
    def live(key, url, **kwargs):
        probes.append((key, url, kwargs, get_hermes_home()))
        return ["fixture-codex-a", "fixture-codex-b"]

    monkeypatch.setattr(catalog, "fetch_api_models", live)
    app = FastAPI()
    app.include_router(models.router)
    scoped_home = tmp_path / "secondary" if secondary else get_hermes_home()
    token = set_hermes_home_override(scoped_home if secondary else None)
    try:
        with TestClient(app) as client:
            response = client.post("/api/providers/clud/setup")
        assert response.status_code == 200, response.text
        assert response.json() == {"provider": "clud-codex", "models": ["fixture-codex-a", "fixture-codex-b"]}
        invocation = json.loads((scoped_home / "invocation.json").read_text())
        assert invocation == {"args": ["install", "hermes", "--configure-only", "--target",
                                       str(setup_clud.install_dir()), "--yes", "--migrate-credentials"],
                              "home": str(scoped_home)}
        assert probes == [("fixture-token", "http://127.0.0.1:8317/v1", {"api_mode": "chat_completions"}, scoped_home)]
        assert load_config()["providers"]["clud-codex"]["api_mode"] == "chat_completions"
        assert catalog.cached_fetch_api_models("fixture-token", "http://127.0.0.1:8317/v1",
                                               api_mode="chat_completions", cache_only=True) == response.json()["models"]
    finally:
        reset_hermes_home_override(token)


@pytest.mark.platforms("posix")
def test_offline_probe_does_not_accept_stale_models(fake_clud, monkeypatch):
    from hermes_cli import models as catalog
    monkeypatch.setattr(catalog, "fetch_api_models", lambda *a, **kw: ["old-model"])
    setup_clud.configure_clud_codex()
    monkeypatch.setattr(catalog, "fetch_api_models", lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match="clud serve --codex"):
        setup_clud.configure_clud_codex()


def test_missing_clud_has_install_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert not setup_clud.clud_available()
    with pytest.raises(RuntimeError, match="Install clud"):
        setup_clud.configure_clud_codex()


@pytest.mark.platforms("posix")
def test_cli_picker_and_setup_select_discovered_model(fake_clud, monkeypatch):
    from hermes_cli import model_setup_flows_custom, models
    from hermes_cli.main_provider_setup import _build_provider_picker_rows

    rows, _ = _build_provider_picker_rows({}, "", {}, {})
    assert ("setup-clud", "Codex subscription via clud", []) in rows
    monkeypatch.setattr(models, "fetch_api_models", lambda *a, **kw: ["fixture-a", "fixture-b"])
    monkeypatch.setattr(model_setup_flows_custom, "_pick_named_custom_model", lambda label, ids, current: ids[-1])
    setup_clud.select_clud_model("")
    assert load_config()["model"]["default"] == "fixture-b"
    assert load_config()["model"]["provider"] == "clud-codex"


def test_cli_picker_hides_missing_clud(tmp_path, monkeypatch):
    from hermes_cli.main_provider_setup import _build_provider_picker_rows
    monkeypatch.setenv("PATH", str(tmp_path))
    rows, _ = _build_provider_picker_rows({}, "", {}, {})
    assert all(row[0] != "setup-clud" for row in rows)


@pytest.mark.platforms("posix")
def test_route_reports_failed_clud_without_exposing_child_output(fake_clud):
    from hermes_cli.web_routers import models
    fake_clud.write_text(f"#!{sys.executable}\nprint('private-child-output')\nraise SystemExit(7)\n")
    app = FastAPI()
    app.include_router(models.router)
    with TestClient(app) as client:
        response = client.post("/api/providers/clud/setup")
    assert response.status_code == 503
    assert "exited 7" in response.json()["detail"]
    assert "private-child-output" not in response.text
