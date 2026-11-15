"""Prepare the brand assets from the source artwork.

Home Assistant / HACS expect:

- ``brand/icon.png`` : 256x256
- ``brand/logo.png`` : 512x256 (wide)

Both keep **transparency**: Home Assistant draws them on light *and* dark themes,
so a baked background is wrong.

When the source already carries an alpha channel it is used untouched. When it
does not — typically a copy that went through a converter which flattened the
transparency onto black — the background is keyed out with a hard threshold plus
a feathered edge, and only the feathered pixels are un-premultiplied. A derived
alpha is never as good as the real one, so prefer feeding the original PNG.

    python tools/build_brand.py <source-image>
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "custom_components" / "thermal_model" / "brand"

ICON_SIZE = 256
LOGO_SIZE = (512, 256)
PADDING_RATIO = 0.08  # of the icon side, so the mark never touches the edge
BLACK_THRESHOLD = 14  # max(R,G,B) at or below this is background
FEATHER_RADIUS = 0.8


def _has_real_alpha(image: Image.Image) -> bool:
    """True when the source carries a usable alpha channel (not just opaque)."""
    if image.mode not in ("RGBA", "LA"):
        return False
    minimum, _maximum = image.convert("RGBA").getchannel("A").getextrema()
    return minimum < 250


def _rgb_pixels(image: Image.Image) -> list[tuple[int, int, int]]:
    """All pixels as RGB triples, on Pillow versions old and new."""
    getter = getattr(image, "get_flattened_data", None) or image.getdata
    return [(int(r), int(g), int(b)) for r, g, b in cast(Any, getter())]


def _gray_pixels(image: Image.Image) -> list[int]:
    """All pixels of an ``L`` image as integers."""
    getter = getattr(image, "get_flattened_data", None) or image.getdata
    return [int(value) for value in cast(Any, getter())]


def _key_out_black(image: Image.Image) -> Image.Image:
    """Derive transparency from a black background without brightening the mark."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    pixels = _rgb_pixels(rgb)

    alpha = Image.new("L", (width, height))
    alpha.putdata(
        [0 if max(red, green, blue) <= BLACK_THRESHOLD else 255 for red, green, blue in pixels]
    )
    alpha = alpha.filter(ImageFilter.GaussianBlur(FEATHER_RADIUS))

    feathered = _gray_pixels(alpha)
    recovered: list[tuple[int, int, int, int]] = []
    for (red, green, blue), alpha_value in zip(pixels, feathered, strict=True):
        if alpha_value == 0:
            recovered.append((0, 0, 0, 0))
        elif alpha_value >= 254:
            recovered.append((red, green, blue, 255))
        else:
            # the pixel is a mix of mark and black: recover the mark
            scale = 255.0 / alpha_value
            recovered.append(
                (
                    min(255, round(red * scale)),
                    min(255, round(green * scale)),
                    min(255, round(blue * scale)),
                    alpha_value,
                )
            )

    result = Image.new("RGBA", (width, height))
    result.putdata(recovered)
    return result


def _prepare_source(path: Path) -> tuple[Image.Image, bool]:
    """Load the source and guarantee an alpha channel; report whether it was native."""
    image = Image.open(path)
    if _has_real_alpha(image):
        return image.convert("RGBA"), True
    return _key_out_black(image), False


def _centred_mark(source: Image.Image, side: int) -> Image.Image:
    """Crop to the visible mark, scale it to fit, centre it on a transparent canvas."""
    bbox = source.getbbox()
    content = source.crop(bbox) if bbox else source

    padding = int(side * PADDING_RATIO)
    target = side - 2 * padding
    scale = min(target / content.width, target / content.height)
    resized = content.resize(
        (max(1, round(content.width * scale)), max(1, round(content.height * scale))),
        Image.Resampling.LANCZOS,
    )

    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(resized, ((side - resized.width) // 2, (side - resized.height) // 2))
    return canvas


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: build_brand.py <source-image>", file=sys.stderr)
        return 2

    source, native_alpha = _prepare_source(Path(argv[1]))
    BRAND.mkdir(parents=True, exist_ok=True)

    icon = _centred_mark(source, ICON_SIZE)
    icon.save(BRAND / "icon.png")

    logo = Image.new("RGBA", LOGO_SIZE, (0, 0, 0, 0))
    mark = _centred_mark(source, LOGO_SIZE[1])
    logo.paste(mark, ((LOGO_SIZE[0] - mark.width) // 2, 0))
    logo.save(BRAND / "logo.png")

    source.save(BRAND / "source.png")

    print(f"alpha: {'native' if native_alpha else 'derived (black background keyed out)'}")
    print(f"icon   {icon.size} -> {BRAND / 'icon.png'}")
    print(f"logo   {logo.size} -> {BRAND / 'logo.png'}")
    print(f"source {source.size} -> {BRAND / 'source.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
