"""``/talos`` serves the phone PWA from the gateway's own listener.

The install link is ``<base>/talos/#k=<key>``: one origin, one port. These tests
hold the properties that make that safe and usable:

- the page served is the built ``index.html``, unmodified — the key comes from
  the fragment, so nothing is injected and nothing is cached;
- hashed assets are immutable, everything else is ``no-store``;
- an unknown path under ``/talos`` returns the app (single-page), while a path
  that escapes the dist never does;
- with nothing built, the route answers 404 with a reason an operator can act
  on rather than a stack trace.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.web_server_talos_messaging import mount_talos_messaging


@pytest.fixture
def client(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Talos Messaging</title>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log('app')", encoding="utf-8")
    (dist / "manifest.webmanifest").write_text('{"name":"Talos Messaging"}', encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the dist", encoding="utf-8")

    app = FastAPI()
    app.state.talos_messaging_dist = dist
    mount_talos_messaging(app)
    return TestClient(app)


def test_the_install_path_serves_the_app_untouched(client):
    for path in ("/talos", "/talos/"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "Talos Messaging" in response.text
        assert response.headers["cache-control"].startswith("no-store")


def test_a_hashed_asset_is_immutable(client):
    response = client.get("/talos/assets/index-abc123.js")
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert "console.log('app')" in response.text


def test_the_manifest_is_served_and_not_cached_forever(client):
    response = client.get("/talos/manifest.webmanifest")
    assert response.status_code == 200
    assert '"Talos Messaging"' in response.text
    assert "no-store" in response.headers["cache-control"]


def test_an_unknown_path_is_the_app_not_a_404(client):
    """Single page: /talos/whatever is the app's own route."""
    response = client.get("/talos/some/app/route")
    assert response.status_code == 200
    assert "Talos Messaging" in response.text


def test_a_path_that_escapes_the_dist_never_serves_a_file(client):
    response = client.get("/talos/../secret.txt")
    assert "outside the dist" not in response.text
    encoded = client.get("/talos/%2e%2e/secret.txt")
    assert "outside the dist" not in encoded.text


def test_nothing_built_is_a_404_with_a_reason(tmp_path):
    app = FastAPI()
    app.state.talos_messaging_dist = tmp_path / "missing"
    mount_talos_messaging(app)
    response = TestClient(app).get("/talos/")
    assert response.status_code == 404
    assert response.headers["x-hermes-reason"] == "talos_messaging_not_built"
    assert "npm run build" in response.json()["error"]


def test_the_default_dist_is_the_apps_build_output():
    from hermes_cli.web_server_talos_messaging import TALOS_MESSAGING_DIST

    assert TALOS_MESSAGING_DIST.name == "dist"
    assert TALOS_MESSAGING_DIST.parent.name == "talos-messaging"
    assert Path(TALOS_MESSAGING_DIST).is_absolute()
