"""Generate the PNG icon set from the Course Ops app icon.

Run when the icon changes:

    python tools/make_icons.py

Reads `docs/brand/source/icon.png` (the full-bleed square the artwork was
delivered as) for the home-screen icons, draws the favicons geometrically,
and writes every raster size the platforms ask for into
`src/courseops/static/`. Pillow only; no native SVG renderer is needed.

Why PNGs at all, when there is already an SVG favicon:

- **iOS ignores SVG for home screen icons.** `apple-touch-icon` must be a PNG,
  and iOS also ignores the web manifest, so without it "Add to Home Screen"
  produces a screenshot of the page instead of an icon.
- **Android maskable icons need a safe zone.** The launcher crops to a shape of
  its choosing, so the mark has to sit inside the central 80%.
- Older browsers still want a 16/32px PNG or ICO favicon.

Two things the source's shape forces:

- **It is full bleed and the road runs off the bottom edge.** iOS and Android
  apply their own corner mask, so the source has square corners; baking a
  radius in shows as a double-rounded edge. When the art is shrunk for the
  maskable safe zone it is anchored to the BOTTOM, so the road still leaves
  the frame instead of stopping at a hard line mid-icon.
- **The navy is snapped to the token first.** The delivered file is a few
  units off `#0B2545`, and padding it with the token colour would show the
  seam. Every pixel is pushed to the nearest of the three brand colours,
  which also removes the model's anti-aliasing fringe.

Favicons are the pin alone, drawn geometrically to the same logical shape
as `static/logo-pin.svg` and `static/favicon.svg`: at 16-48px the road is
noise, and cropping the source cannot lose it because it runs behind the
pin's tip. At 16px on a 1x display the pin is still a smudge - a tower and
arcs inside a ring is more than three features - and that is accepted for
consistency with the home-screen icon; hi-DPI screens, which is most of
them, get the 32px rendering.
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "brand" / "source" / "icon.png"
STATIC = ROOT / "src" / "courseops" / "static"

NAVY = (11, 37, 69, 255)
ORANGE = (255, 106, 19, 255)
WHITE = (255, 255, 255, 255)
BRAND = (NAVY, ORANGE, WHITE)

SUPERSAMPLE = 4

# The hand-drawn pin lives in the 64x80 logical box of logo-pin.svg.
PIN_HEIGHT = 80.0


def _snap(r: int, g: int, b: int, a: int) -> tuple[int, int, int, int]:
    if a < 128:
        return NAVY
    return min(BRAND, key=lambda c: (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2)


def load_source() -> Image.Image:
    image = Image.open(SOURCE).convert("RGBA")
    raw = image.tobytes()
    cache: dict[bytes, bytes] = {}
    out = bytearray()
    for i in range(0, len(raw), 4):
        px = raw[i : i + 4]
        hit = cache.get(px)
        if hit is None:
            hit = cache[px] = bytes(_snap(*px))
        out += hit
    return Image.frombytes("RGBA", image.size, bytes(out))


def draw_pin(draw: ImageDraw.ImageDraw, size: float, height: float) -> None:
    """The logo-pin.svg mark, `height` tall, centred in a `size` square."""
    k = height / PIN_HEIGHT

    def x(v: float) -> float:
        return size / 2 + (v - 32.0) * k

    def y(v: float) -> float:
        return size / 2 + (v - 40.0) * k

    # Ring: circle plus a triangle to the tip, the join hidden inside the circle.
    r = 28.0 * k
    draw.ellipse([x(32) - r, y(30) - r, x(32) + r, y(30) + r], fill=WHITE)
    draw.polygon([(x(8), y(44)), (x(56), y(44)), (x(32), y(78))], fill=WHITE)
    d = 21.0 * k
    draw.ellipse([x(32) - d, y(30) - d, x(32) + d, y(30) + d], fill=NAVY)

    # Signal arcs: two a side, centred on the tower's ball.
    w = max(1, int(round(4.0 * k)))
    for radius, half in ((10.0, 48.6), (17.0, 47.5)):
        rr = radius * k
        box = [x(32) - rr, y(30) - rr, x(32) + rr, y(30) + rr]
        draw.arc(box, 180 - half, 180 + half, fill=ORANGE, width=w)
        draw.arc(box, -half, half, fill=ORANGE, width=w)

    # Tower: ball, two legs, three braces and the cross-bracing between them.
    b = 3.2 * k
    draw.ellipse([x(32) - b, y(18) - b, x(32) + b, y(18) + b], fill=WHITE)
    lw = max(1, int(round(2.4 * k)))
    for a, c in (
        ((32, 21), (25, 48)), ((32, 21), (39, 48)),
        ((28.6, 34), (35.4, 34)), ((27, 40.5), (37, 40.5)), ((25, 48), (39, 48)),
        ((28.6, 34), (37, 40.5)), ((35.4, 34), (27, 40.5)),
        ((27, 40.5), (39, 48)), ((37, 40.5), (25, 48)),
    ):
        draw.line([(x(a[0]), y(a[1])), (x(c[0]), y(c[1]))], fill=WHITE, width=lw)


def frame(size: int, radius_ratio: float) -> tuple[Image.Image, ImageDraw.ImageDraw, int]:
    big = size * SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if radius_ratio > 0:
        draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=big * radius_ratio, fill=NAVY)
    else:
        draw.rectangle([0, 0, big - 1, big - 1], fill=NAVY)
    return image, draw, big


def render_icon(source: Image.Image, size: int, scale: float) -> Image.Image:
    """The whole icon, full bleed, `scale` of the frame wide, bottom-anchored."""
    image, _, big = frame(size, 0.0)
    width = int(round(big * scale))
    height = int(round(width * source.height / source.width))
    art = source.resize((width, height), Image.LANCZOS)
    image.alpha_composite(art, ((big - width) // 2, big - height))
    return image.resize((size, size), Image.LANCZOS)


def render_favicon(size: int) -> Image.Image:
    """The pin alone on a rounded navy square, matching favicon.svg."""
    image, draw, big = frame(size, 0.22)
    draw_pin(draw, big, big * 0.82)
    return image.resize((size, size), Image.LANCZOS)


# (filename, size, art width as a fraction of the frame)
ICONS = [
    # iOS home screen. Full bleed - iOS applies its own squircle mask.
    ("apple-touch-icon.png", 180, 1.0),
    # Android / PWA manifest.
    ("icon-192.png", 192, 1.0),
    ("icon-512.png", 512, 1.0),
    # Maskable: the pin kept inside the central 80% safe zone.
    ("icon-maskable-192.png", 192, 0.84),
    ("icon-maskable-512.png", 512, 0.84),
]
FAVICONS = [16, 32, 48]


def main() -> None:
    source = load_source()
    for size in FAVICONS:
        name = f"favicon-{size}.png"
        path = STATIC / name
        render_favicon(size).save(path, "PNG", optimize=True)
        print(f"{name:26} {size:>4}px  {path.stat().st_size:>6,} bytes")
    for name, size, scale in ICONS:
        path = STATIC / name
        render_icon(source, size, scale).save(path, "PNG", optimize=True)
        print(f"{name:26} {size:>4}px  {path.stat().st_size:>6,} bytes")

    # Multi-resolution ICO for browsers that still ask for /favicon.ico.
    ico_sizes = [(16, 16), (32, 32), (48, 48)]
    base = render_favicon(48)
    base.save(STATIC / "favicon.ico", sizes=ico_sizes)
    print(f"{'favicon.ico':26}       {(STATIC / 'favicon.ico').stat().st_size:>6,} bytes")


if __name__ == "__main__":
    main()
