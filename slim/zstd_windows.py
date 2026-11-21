#!/usr/bin/env python3
"""Fetch the standalone Windows zstd decompressor that install.ps1 unpacks bundles with.

    python slim/zstd_windows.py OUT_DIR

Writes OUT_DIR/zstd-win32-x64.exe from facebook/zstd's official win64 release, pinned by
sha256. It is unsigned upstream; CI Authenticode-signs it with the bundle (it runs on the
user's machine before anything else in the release does) and only then writes its
.sha256, so the checksum describes the signed bytes. slim/build_zstd.sh builds the macOS
and Linux helpers from source; #130 tracks building this one the same way.
"""

from __future__ import annotations

import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = "1.5.7"
URL = f"https://github.com/facebook/zstd/releases/download/v{VERSION}/zstd-v{VERSION}-win64.zip"
SHA256 = "acb4e8111511749dc7a3ebedca9b04190e37a17afeb73f55d4425dbf0b90fad9"
MEMBER = f"zstd-v{VERSION}-win64/zstd.exe"
NAME = "zstd-win32-x64.exe"


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    out = Path(sys.argv[1])
    with urllib.request.urlopen(URL, timeout=120) as resp:
        data = resp.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        raise SystemExit(f"sha256 mismatch for {URL}: {digest}")
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        (out / NAME).write_bytes(z.read(MEMBER))
    print(f"{out / NAME} from zstd {VERSION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
