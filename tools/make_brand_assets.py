"""Generate the HACS brand assets (icon + logo) for the integration.

Pure geometric drawing, no third-party asset: a house outline with a
thermometer inside it. Run with any Python that has Pillow installed:

    python tools/make_brand_assets.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "custom_components" / "thermal_model" / "brand"

BACKGROUND = (23, 43, 71, 255)  # deep blue
WALL = (255, 255, 255, 255)
ACCENT_HOT = (255, 138, 76, 255)  # warm orange
ACCENT_COLD = (86, 182, 255, 255)  # cool blue
MERCI = (255, 255, 255, 255)


def _house(draw: ImageDraw.ImageDraw, size: int, inset: int) -> None:
    """Draw a house outline centred in a square canvas."""
    left = inset
    right = size - inset
    roof_top = inset + int(size * 0.06)
    roof_bottom = int(size * 0.46)
    wall_bottom = size - inset
    width = max(4, size // 26)

    draw.polygon(
        [(left, roof_bottom), (size // 2, roof_top), (right, roof_bottom)],
        outline=WALL,
    )
    draw.line(
        [(left + width, roof_bottom), (left + width, wall_bottom)],
        fill=WALL,
        width=width,
    )
    draw.line(
        [(right - width, roof_bottom), (right - width, wall_bottom)],
        fill=WALL,
        width=width,
    )
    draw.line(
        [(left + width, wall_bottom), (right - width, wall_bottom)],
        fill=WALL,
        width=width,
    )


def _thermometer(draw: ImageDraw.ImageDraw, size: int) -> None:
    """Draw a thermometer whose column is half hot, half cold."""
    cx = size // 2
    tube_top = int(size * 0.30)
    bulb_centre = int(size * 0.76)
    bulb_radius = int(size * 0.075)
    tube_half = max(4, size // 34)

    draw.rounded_rectangle(
        [(cx - tube_half, tube_top), (cx + tube_half, bulb_centre)],
        radius=tube_half,
        fill=MERCI,
    )
    draw.ellipse(
        [
            (cx - bulb_radius, bulb_centre - bulb_radius),
            (cx + bulb_radius, bulb_centre + bulb_radius),
        ],
        fill=ACCENT_HOT,
    )
    # half-filled column: the hotter the reading, the warmer the colour
    middle = (tube_top + bulb_centre) // 2
    draw.rounded_rectangle(
        [(cx - tube_half, middle), (cx + tube_half, bulb_centre)],
        radius=tube_half,
        fill=ACCENT_HOT,
    )
    draw.rounded_rectangle(
        [(cx - tube_half, tube_top), (cx + tube_half, middle)],
        radius=tube_half,
        fill=ACCENT_COLD,
    )
    # graduation ticks
    tick = max(3, size // 42)
    for index in range(3):
        y = tube_top + int((middle - tube_top) * (index + 1) / 4)
        draw.line(
            [(cx + tube_half + tick, y), (cx + tube_half + 3 * tick, y)],
            fill=WALL,
            width=tick,
        )


def build_icon(size: int = 256) -> Image.Image:
    """Square icon for HACS / the integration card."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([(0, 0), (size - 1, size - 1)], radius=size // 6, fill=BACKGROUND)
    _house(draw, size, inset=int(size * 0.14))
    _thermometer(draw, size)
    return image


def build_logo(width: int = 512, height: int = 256) -> Image.Image:
    """Wide logo for the HACS repository page."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle([(0, 0), (width - 1, height - 1)], radius=height // 8, fill=BACKGROUND)
    square = build_icon(size=height - 32)
    image.alpha_composite(square, (16, 16))
    return image


def main() -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    build_icon().save(BRAND / "icon.png")
    build_logo().save(BRAND / "logo.png")
    print(f"written: {BRAND / 'icon.png'} and {BRAND / 'logo.png'}")


if __name__ == "__main__":
    main()
