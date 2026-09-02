"""APRS symbol interpretation.

A station's symbol is the most reliable machine-readable clue to what it *is*.
That matters at check-in time: a club member's callsign often has several SSIDs
on the air at once - the phone they will carry, plus a digipeater or igate they
run at home - and only one of them belongs on the roster.

The symbol is a table character plus a code character, and the pair must be read
together: the table changes what the code means. `/#` is a digipeater, `\\#` is
an overlaid digi, and the code alone says nothing.

This is a partial table, covering what turns up around an event. Anything not
listed is reported as unknown rather than guessed at.
"""

from __future__ import annotations

# Primary table ("/"). code -> (description, is_infrastructure)
#
# is_infrastructure means "a fixed station, not a person moving around a
# course" - the things you almost never want on an event roster.
_PRIMARY = {
    "!": ("Police/emergency", False),
    "#": ("Digipeater", True),
    "&": ("Igate / gateway", True),
    "-": ("House / home station", True),
    ">": ("Car", False),
    "[": ("Person / jogger", False),
    "b": ("Bicycle", False),
    "j": ("Jeep", False),
    "k": ("Truck", False),
    "u": ("Truck (18-wheeler)", False),
    "v": ("Van", False),
    "<": ("Motorcycle", False),
    "a": ("Ambulance", False),
    "f": ("Fire truck", False),
    "h": ("Hospital", True),
    "+": ("Red Cross", False),
    "r": ("Repeater", True),
    "Y": ("Yacht / boat", False),
    "_": ("Weather station", True),
    "$": ("Phone", False),
    "'": ("Aircraft (small)", False),
    "O": ("Balloon", False),
    "s": ("Boat / ship", False),
}

# Alternate table ("\\"). Only the few that matter here.
_ALTERNATE = {
    "#": ("Digipeater (overlaid)", True),
    "&": ("Igate (overlaid)", True),
    "n": ("Network node", True),
    "r": ("Repeater (overlaid)", True),
}


def describe(table: str | None, code: str | None) -> str:
    """Human description of a symbol pair, or 'unknown'."""
    if not table or not code:
        return "unknown"
    lookup = _ALTERNATE if table == "\\" else _PRIMARY
    entry = lookup.get(code)
    if entry is None and lookup is _ALTERNATE:
        entry = _PRIMARY.get(code)
    return entry[0] if entry else f"unknown ({table}{code})"


def is_infrastructure(table: str | None, code: str | None) -> bool:
    """True for fixed plant - digipeaters, igates, home and weather stations.

    Used to flag SSIDs that turn up under a rostered callsign but almost
    certainly should not be tracked as a person on the course.
    """
    if not table or not code:
        return False
    lookup = _ALTERNATE if table == "\\" else _PRIMARY
    entry = lookup.get(code) or (_PRIMARY.get(code) if lookup is _ALTERNATE else None)
    return bool(entry and entry[1])
