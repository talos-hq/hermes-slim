"""Serve Talos Messaging — the phone PWA — from the gateway's own listener.

The install link is ``<base>/talos/#k=<key>``: one origin, one port, no second
server. This module mounts that path, registered before the dashboard's
catch-all so ``/talos/…`` never falls into the desktop SPA.

It injects nothing. The dashboard injects its session token into ``index.html``;
Talos Messaging takes its key from the URL **fragment**, which the browser never
sends to the server, so the served page is byte-for-byte the built one.

A missing build is not a mount-time failure: every route reads the dist per
request, so ``npm run build`` in ``apps/talos-messaging`` is picked up without a
gateway restart (the dashboard's ``--skip-build`` recovery, same reasoning).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response

#: ``<repository root>/apps/talos-messaging/dist``, the Vite output.
TALOS_MESSAGING_DIST = Path(__file__).parent.parent / "apps" / "talos-messaging" / "dist"
BASE_PATH = "/talos"
_NOT_BUILT = "talos_messaging_not_built"
#: Hashed bundle assets never change under a name; index.html must never be cached.
_IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}
_NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate"}


def _dist(application: FastAPI) -> Path:
    return getattr(application.state, "talos_messaging_dist", None) or TALOS_MESSAGING_DIST


def _inside(root: Path, candidate: Path) -> bool:
    """True when *candidate* stays inside *root* after resolving links and ``..``."""
    try:
        candidate.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def mount_talos_messaging(application: FastAPI) -> None:
    """Mount the built app at ``/talos``. Safe to call when nothing is built yet."""

    @application.get(f"{BASE_PATH}")
    @application.get(f"{BASE_PATH}/")
    async def talos_messaging_index() -> Response:
        return _index(application)

    @application.get(f"{BASE_PATH}/{{asset_path:path}}")
    async def talos_messaging_asset(asset_path: str) -> Response:
        root = _dist(application)
        candidate = root / asset_path
        if asset_path and _inside(root, candidate) and candidate.is_file():
            headers = _IMMUTABLE if asset_path.startswith("assets/") else _NO_STORE
            return FileResponse(candidate, headers=headers)
        # A single-page app: an unknown path is the app's own route, not a 404.
        return _index(application)


def _index(application: FastAPI) -> Response:
    index = _dist(application) / "index.html"
    if not index.is_file():
        # FileResponse raises only when it starts sending, so the check is here.
        return JSONResponse(
            {"error": "Talos Messaging is not built: run `npm run build` in apps/talos-messaging"},
            status_code=404,
            headers={"X-Hermes-Reason": _NOT_BUILT},
        )
    return FileResponse(index, media_type="text/html", headers=_NO_STORE)
