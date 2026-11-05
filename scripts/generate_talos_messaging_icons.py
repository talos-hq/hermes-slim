#!/usr/bin/env python3
"""Render the Talos Messaging home-screen icons from one SVG.

iOS gives a home-screen app no icon of its own unless the page declares an
``apple-touch-icon``; Android/Chrome read the manifest. Both come from
``apps/talos-messaging/public/icons/talos-messaging.svg`` so a rebrand moves all
of them at once, and the maskable variant keeps every mark inside the safe zone
Android may crop to a circle.

Usage (from the repo root):
    uv run --frozen --group dev python scripts/generate_talos_messaging_icons.py
    uv run --frozen --group dev python scripts/generate_talos_messaging_icons.py --check
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import resvg_py
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "apps" / "talos-messaging" / "public" / "icons"
SOURCE = ICONS / "talos-messaging.svg"
BACKGROUND = (0x0B, 0x0D, 0x10, 0xFF)

#: (filename, side, maskable). A maskable icon is drawn smaller on the same
#: obsidian square so Android's circular crop cannot clip the ring.
TARGETS: tuple[tuple[str, int, bool], ...] = (
    ("apple-touch-icon.png", 180, False),
    ("icon-192.png", 192, False),
    ("icon-512.png", 512, False),
    ("icon-maskable-512.png", 512, True),
)
MASKABLE_INSET = 0.82


def render(svg: str, side: int, maskable: bool) -> Image.Image:
    png = bytes(resvg_py.svg_to_bytes(svg_string=svg, width=side, height=side))
    image = Image.open(__import__("io").BytesIO(png)).convert("RGBA")
    canvas = Image.new("RGBA", (side, side), BACKGROUND)
    if maskable:
        inset = int(side * MASKABLE_INSET)
        offset = (side - inset) // 2
        canvas.alpha_composite(image.resize((inset, inset), Image.LANCZOS), (offset, offset))
    else:
        canvas.alpha_composite(image)
    return canvas


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify the icons exist and match the source; write nothing")
    args = parser.parse_args(argv)
    if not SOURCE.is_file():
        print(f"missing icon source: {SOURCE}", file=sys.stderr)
        return 1
    svg = SOURCE.read_text(encoding="utf-8")
    missing = [name for name, _side, _mask in TARGETS if not (ICONS / name).is_file()]
    if args.check:
        if missing:
            print("missing icons: " + ", ".join(missing), file=sys.stderr)
            return 1
        print(f"icons ok ({len(TARGETS)} files)")
        return 0
    ICONS.mkdir(parents=True, exist_ok=True)
    for name, side, maskable in TARGETS:
        image = render(svg, side, maskable)
        image.save(ICONS / name, format="PNG", optimize=True)
        print(f"wrote {ICONS / name} ({side}x{side}{', maskable' if maskable else ''})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
