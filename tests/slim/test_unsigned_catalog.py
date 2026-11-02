"""The Windows signing catalog (unsigned.txt) lists binaries the way azure/artifact-signing-action
reads them: each line resolved against the catalog's own folder."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _build_bundle():
    spec = importlib.util.spec_from_file_location("slim_build_bundle", ROOT / "slim" / "build_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_catalog_line_resolves_from_the_catalog_folder(tmp_path):
    work = tmp_path / "hs-work"
    pyd = work / "hermes-slim" / "python" / "Lib" / "site-packages" / "_cffi_backend.cp314-win_amd64.pyd"
    pyd.parent.mkdir(parents=True)
    pyd.write_bytes(b"not a signed PE")

    _build_bundle().write_unsigned_list(work / "hermes-slim", work)

    catalog = work / "unsigned.txt"
    lines = catalog.read_text().splitlines()
    assert lines
    for line in lines:
        assert not Path(line).is_absolute(), line
        assert (catalog.parent / line).is_file(), line
