"""A read-only tool store that ships inside the install (the hermes-slim bundle's pm-store, #142).

The install stamp names it (``bundledStore``, relative to the stamp). PM's lookups and
``ensure`` must find a tool there and download nothing; without the stamp field the same
tree is invisible and PM downloads as before.
"""

from __future__ import annotations

import json
import shutil

import pytest

import pm
from pm import paths
from pm.install import ensure
from tests.pm.test_pm_core import pm_env as pm_env  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import
from tests.pm.test_pm_core import served as served  # noqa: PLC0414 -- pm_env's own fixture dependency, resolved from this module


def _bundle(tmp_path, monkeypatch, runtime, docroot, *, named: bool):
    ensure("faketool", explicit=True)  # build time: PM's own installer fills the store
    bundle = tmp_path / "bundle"
    shutil.copytree(runtime, bundle / "pm-store")
    shutil.rmtree(runtime)  # the client's own store starts empty
    for archive in docroot.glob("faketool-*"):
        archive.unlink()  # any download attempt now fails
    app = bundle / "app"
    app.mkdir()
    (app / "install-stamp.json").write_text(json.dumps({"bundledStore": "../pm-store"} if named else {}))
    monkeypatch.delenv("HERMES_INSTALL_ROOT", raising=False)
    monkeypatch.setattr(paths, "repo_root", lambda: app)
    return bundle


def test_stamped_bundle_store_serves_tools_without_a_download(pm_env, tmp_path, monkeypatch):
    _lock, runtime, docroot, _url = pm_env
    bundle = _bundle(tmp_path, monkeypatch, runtime, docroot, named=True)

    installed = pm.installed_package("faketool")
    assert installed is not None
    assert installed.binary.is_relative_to(bundle / "pm-store")
    ensure("faketool")  # satisfied by the bundle: no download, nothing written to the client store
    assert not (runtime / "facts.json").exists()
    assert str(bundle / "pm-store") in pm.env_for("faketool")["PATH"]


def test_unstamped_tree_is_not_a_store(pm_env, tmp_path, monkeypatch):
    _lock, runtime, docroot, _url = pm_env
    _bundle(tmp_path, monkeypatch, runtime, docroot, named=False)

    assert pm.installed_package("faketool") is None
    with pytest.raises(Exception):
        ensure("faketool")  # falls through to a download, and the archive is gone
