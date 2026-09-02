"""Course line styling: color and dash pattern.

Two constraints drive the defaults, and both come from the event rather than
from taste:

1. **Sunlight.** The map is read on a phone outdoors in daylight. Pale or
   low-saturation lines disappear on light OSM raster tiles.
2. **Overlap.** Marathon courses share road — the Full, Half and 10K commonly
   run the same miles before splitting, so their lines are coincident.

Overlap is handled by **draw order**, which is adjustable per course
(`course.sort_order`): the operator chooses which route wins on shared road.
Courses are therefore solid by default; nothing is imposed.

Dash patterns remain available as an opt-in for the case draw order cannot
cover — seeing two coincident routes at once, since a dashed line on top lets
the solid line underneath show through its gaps. Set one explicitly when that
is what you want.

Colors are the Okabe-Ito colorblind-safe palette, minus the yellow, which
vanishes against light map tiles. Roughly one man in twelve has some red-green
color deficiency, and on a course map that matters more than usual.
"""

from __future__ import annotations

import re

# Okabe-Ito, ordered for maximum separation between the first few courses and
# filtered for legibility on light raster tiles.
DEFAULT_COLORS = [
    "#D55E00",  # vermillion
    "#0072B2",  # blue
    "#009E73",  # bluish green
    "#CC79A7",  # reddish purple
    "#E69F00",  # orange
    "#000000",  # black
]

# SVG stroke-dasharray values, passed straight to Leaflet's `dashArray`.
# Opt-in presets, not defaults: courses are solid unless asked otherwise.
DASH_PRESETS: dict[str, str | None] = {
    "solid": None,
    "long": "12,8",
    "dotted": "3,7",
    "dash-dot": "18,6,4,6",
    "medium": "8,6",
}

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_DASH = re.compile(r"^\d{1,3}(?:\s*,\s*\d{1,3})*$")


def is_valid_color(value: str | None) -> bool:
    return bool(value and _HEX_COLOR.match(value.strip()))


def normalize_color(value: str | None) -> str | None:
    return value.strip().lower() if is_valid_color(value) else None


def is_valid_dash(value: str | None) -> bool:
    """Accept a preset name, an SVG dasharray, or 'solid'/'none'."""
    if value is None:
        return True
    text = value.strip().lower()
    return (
        text in DASH_PRESETS
        or text in {"", "none"}
        or bool(_DASH.match(text))
    )


def normalize_dash(value: str | None) -> str | None:
    """Returns None for a solid line, else a cleaned dasharray."""
    if value is None:
        return None
    text = value.strip().lower()
    if text in DASH_PRESETS:
        return DASH_PRESETS[text]
    if text in {"", "none"}:
        return None
    if not _DASH.match(text):
        return None
    return ",".join(part.strip() for part in text.split(","))


def next_color(used_colors: list[str | None]) -> str:
    """Pick an unused palette color for a new course.

    Falls back to reusing one once the palette is exhausted, since a seventh
    course is far beyond a normal event and a duplicate beats a crash.
    """
    taken = {c.lower() for c in used_colors if c}
    return next(
        (c for c in DEFAULT_COLORS if c.lower() not in taken),
        DEFAULT_COLORS[len(taken) % len(DEFAULT_COLORS)],
    )


def describe_dash(value: str | None) -> str:
    """Human label for the CLI listing."""
    if value is None:
        return "solid"
    reverse = {v: k for k, v in DASH_PRESETS.items() if v}
    return reverse.get(value, value)
