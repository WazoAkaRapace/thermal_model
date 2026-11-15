"""Prepare the official brand assets from the provided logo.

The source is a square JPEG (1254x1254) with a black background and a drawing
whose bounding box is off-centre. Home Assistant / HACS expect:

- ``brand/icon.png`` : 256x256, drawing centred with even padding
- ``brand/logo.png`` : 512x256, the mark centred (there is no wordmark)

The original is kept as ``brand/source.png`` so the assets can be regenerated.

    /home/hermes/.hermes/hermes-agent/venv/bin/python tools/build_brand.py <source>
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "custom_components" / "thermal_model" / "brand"

ICON_SIZE = 256
LOGO_SIZE = (512, 256)
PADDING_RATIO = 0.08  # of the icon side, so the mark never touches the edge
BACKGROUND = (0, 0, 0, 255)


def _centred_mark(source: Image.Image, side: int) -> Image.Image:
    """Crop to the drawing, scale it to fit, and centre it on a square canvas."""
    rgb = source.convert("RGB")
    bbox = rgb.getbbox()
    content = rgb.crop(bbox)

    padding = int(side * PADDING_RATIO)
    target = side - 2 * padding
    scale = min(target / content.width, target / content.height)
    resized = content.resize(
        (max(1, round(content.width * scale)), max(1, round(content.height * scale))),
        Image.Resampling.LANCZOS,
    )

    canvas = Image.new("RGBA", (side, side), BACKGROUND)
    canvas.paste(
        resized.convert("RGBA"),
        ((side - resized.width) // 2, (side - resized.height) // 2),
    )
    return canvas


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: build_brand.py <source-image>", file=sys.stderr)
        return 2

    source = Image.open(argv[1])
    BRAND.mkdir(parents=True, exist_ok=True)

    icon = _centred_mark(source, ICON_SIZE)
    icon.save(BRAND / "icon.png")

    logo = Image.new("RGBA", LOGO_SIZE, BACKGROUND)
    mark = _centred_mark(source, LOGO_SIZE[1])
    logo.paste(mark, ((LOGO_SIZE[0] - mark.width) // 2, 0))
    logo.save(BRAND / "logo.png")

    # keep the full resolution source inside the repo (regeneration + brands repo)
    source.convert("RGBA").save(BRAND / "source.png")

    print(f"icon  {icon.size}  -> {BRAND / 'icon.png'}")
    print(f"logo  {logo.size}  -> {BRAND / 'logo.png'}")
    print(f"source {source.size} -> {BRAND / 'source.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
