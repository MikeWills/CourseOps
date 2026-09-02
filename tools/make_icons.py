"""Generate the PNG icon set from the Course Ops mark.

Run when the logo changes:

    pip install pillow
    python tools/make_icons.py

Pillow is NOT a runtime dependency — it is only needed to regenerate these
files, and the generated PNGs are committed. Nothing the app serves imports it.

Why PNGs at all, when there is already an SVG favicon:

- **iOS ignores SVG for home screen icons.** `apple-touch-icon` must be a PNG,
  and iOS does not read the web manifest for it either. Without this file, a
  volunteer who adds Course Ops to their home screen gets a blurry screenshot of
  the page instead of an icon.
- **Android maskable icons need a safe zone.** The launcher crops to a shape of
  its choosing and only guarantees the central 80%, so the maskable variants
  draw the mark smaller. That is deliberate.
- Older browsers still want a 16/32px PNG or ICO favicon.

The mark is drawn geometrically rather than by rasterising the SVG, so this
script needs no native rendering library. Everything is supersampled 4x and
downsampled with LANCZOS, which is what keeps the curves clean at 16px.
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw

STATIC = pathlib.Path(__file__).resolve().parents[1] / "src" / "courseops" / "static"

NAVY = (11, 37, 69, 255)
ORANGE = (255, 106, 19, 255)
DISC = (247, 249, 252, 255)

SUPERSAMPLE = 4

# Logical 64x64 mark, matching favicon.svg.
PIN_CENTRE = (32.0, 26.0)
PIN_RADIUS = 17.0
PIN_TIP_Y = 55.0
DISC_RADIUS = 11.5
# Kept clear of the disc edge: at the previous amplitude the pulse touched the
# rim on all four sides and read as a bolt rather than a trace.
WAVE = [(23.5, 26), (26.5, 26), (29.2, 19.5), (34.8, 32.5), (37.5, 26), (40.5, 26)]
WAVE_WIDTH = 4.0


def draw_mark(draw: ImageDraw.ImageDraw, size: float, scale: float, cy_shift: float) -> None:
    """Draw the pin centred in a `size` square, at `scale` relative to 64px."""
    def x(v: float) -> float:
        return size / 2 + (v - 32.0) * scale

    def y(v: float) -> float:
        return size / 2 + cy_shift + (v - 32.0) * scale

    # Teardrop = circle plus a triangle running down to the tip. The triangle's
    # top edge is chosen to sit inside the circle so the join is invisible.
    cx, cy = PIN_CENTRE
    r = PIN_RADIUS * scale
    draw.ellipse([x(cx) - r, y(cy) - r, x(cx) + r, y(cy) + r], fill=ORANGE)
    spread = PIN_RADIUS * 0.86
    draw.polygon(
        [
            (x(cx - spread), y(cy + 4.0)),
            (x(cx + spread), y(cy + 4.0)),
            (x(cx), y(PIN_TIP_Y)),
        ],
        fill=ORANGE,
    )

    dr = DISC_RADIUS * scale
    draw.ellipse([x(cx) - dr, y(cy) - dr, x(cx) + dr, y(cy) + dr], fill=DISC)

    # Round caps and joins: Pillow has no stroke-linejoin, so a disc is stamped
    # at every vertex. Without this the sharp peaks of the pulse look chipped.
    width = max(1, int(round(WAVE_WIDTH * scale)))
    points = [(x(px), y(py)) for px, py in WAVE]
    draw.line(points, fill=NAVY, width=width, joint="curve")
    for px, py in points:
        rr = width / 2
        draw.ellipse([px - rr, py - rr, px + rr, py + rr], fill=NAVY)


def render(size: int, scale: float, cy_shift: float, radius_ratio: float) -> Image.Image:
    big = size * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if radius_ratio > 0:
        draw.rounded_rectangle(
            [0, 0, big - 1, big - 1], radius=big * radius_ratio, fill=NAVY
        )
    else:
        # Full bleed: iOS and Android apply their own mask, and baking in a
        # different corner radius shows as a double-rounded edge.
        draw.rectangle([0, 0, big - 1, big - 1], fill=NAVY)

    draw_mark(draw, big, scale * SUPERSAMPLE, cy_shift * SUPERSAMPLE)
    return image.resize((size, size), Image.LANCZOS)


# (filename, size, mark scale relative to the 64px mark, vertical nudge, corner radius)
TARGETS = [
    # Browser favicons: own rounded corners, shown unmasked.
    ("favicon-16.png", 16, 0.22, 0.0, 0.22),
    ("favicon-32.png", 32, 0.44, 0.0, 0.22),
    ("favicon-48.png", 48, 0.66, 0.0, 0.22),
    # iOS home screen. Full bleed - iOS applies its own squircle mask.
    ("apple-touch-icon.png", 180, 2.3, 3.0, 0.0),
    # Android / PWA manifest.
    ("icon-192.png", 192, 2.5, 3.0, 0.0),
    ("icon-512.png", 512, 6.6, 8.0, 0.0),
    # Maskable: mark kept inside the central 80% safe zone.
    ("icon-maskable-192.png", 192, 1.85, 2.0, 0.0),
    ("icon-maskable-512.png", 512, 4.9, 6.0, 0.0),
]


def main() -> None:
    for name, size, scale, shift, radius in TARGETS:
        image = render(size, scale, shift, radius)
        path = STATIC / name
        image.save(path, "PNG", optimize=True)
        print(f"{name:26} {size:>4}px  {path.stat().st_size:>6,} bytes")

    # Multi-resolution ICO for browsers that still ask for /favicon.ico.
    ico_sizes = [(16, 16), (32, 32), (48, 48)]
    base = render(48, 0.66, 0.0, 0.22)
    base.save(STATIC / "favicon.ico", sizes=ico_sizes)
    print(f"{'favicon.ico':26}       {(STATIC / 'favicon.ico').stat().st_size:>6,} bytes")


if __name__ == "__main__":
    main()
