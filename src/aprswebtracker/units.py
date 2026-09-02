"""Unit conversion.

Storage is canonical metric — aprslib normalizes to km/h and meters, and that
is what lands in SQLite. Conversion happens at the display edge only, so the
database never depends on who ran the importer or where they live.

US customary is the default presentation: this is built for US road races,
where the course is measured in miles and everyone on the net speaks mph.
"""

from __future__ import annotations

KM_PER_MILE = 1.609344
METERS_PER_MILE = 1609.344
METERS_PER_FOOT = 0.3048


def kmh_to_mph(kmh: float | None) -> float | None:
    return None if kmh is None else kmh / KM_PER_MILE


def meters_to_feet(meters: float | None) -> float | None:
    return None if meters is None else meters / METERS_PER_FOOT


def meters_to_miles(meters: float | None) -> float | None:
    return None if meters is None else meters / METERS_PER_MILE


def miles_to_meters(miles: float | None) -> float | None:
    return None if miles is None else miles * METERS_PER_MILE


def format_speed(kmh: float | None, imperial: bool = True) -> str:
    if kmh is None:
        return "--"
    return f"{kmh_to_mph(kmh):.0f} mph" if imperial else f"{kmh:.0f} km/h"


def format_altitude(meters: float | None, imperial: bool = True) -> str:
    if meters is None:
        return "--"
    return f"{meters_to_feet(meters):,.0f} ft" if imperial else f"{meters:,.0f} m"


def format_distance(meters: float | None, imperial: bool = True) -> str:
    """Course distances — 'mile 14.2' is the unit the net actually speaks."""
    if meters is None:
        return "--"
    return f"{meters_to_miles(meters):.1f} mi" if imperial else f"{meters / 1000:.1f} km"
