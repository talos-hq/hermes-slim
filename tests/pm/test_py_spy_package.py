"""The profiler wheel becomes a standalone package-manager executable."""

from zipfile import ZipFile

import pytest

from pm import get_package


@pytest.mark.parametrize(
    "target, executable",
    [("linux-x64-musl", "py-spy"), ("win32-arm64", "py-spy.exe")],
)
def test_profiler_wheel_stages_executable_and_license(tmp_path, target, executable):
    package = get_package("py-spy")
    archive = tmp_path / "py-spy.whl"
    with ZipFile(archive, "w") as wheel:
        wheel.writestr(f"py_spy-0.4.4.data/scripts/{executable}", b"profiler executable")
        wheel.writestr("py_spy-0.4.4.dist-info/licenses/LICENSE", b"MIT license")
        wheel.writestr("py_spy-0.4.4.dist-info/RECORD", b"wheel metadata")

    staged = tmp_path / "staged"
    package.unpack(archive, staged, target)

    binary = package.binary(staged, target)
    assert binary.name == executable
    assert binary.read_bytes() == b"profiler executable"
    assert (staged / "LICENSE").read_bytes() == b"MIT license"
    assert set(staged.iterdir()) == {binary, staged / "LICENSE"}
