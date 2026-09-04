"""A one- or two-character label for a place, drawn on the pin itself.

A map of identical pins does not answer the question NCS actually asks, which
is "which one is that?". Hovering each pin in turn to find out is fine at a
desk and useless during a net.

Two characters is the whole budget: a map marker is about 24 pixels, and text
that does not fit is worse than no text. So the label is not an abbreviation of
the name, it is the part of the name that tells two places apart:

    "Aid 3"          -> 3      the number is the identity
    "MM 15 (FULL)"   -> 15
    "Water Stop 4"   -> 4
    "Charlie"        -> C      the letter is the identity
    "Aid Charlie"    -> C      "Aid" describes the kind, not which one
    "Start"          -> S

That last rule is the one that matters. Clubs name stations "Aid 1", "Aid 2",
"Aid 3" or "Alpha", "Bravo", "Charlie" - and taking the first letter blindly
labels every pin on the course "A", which looks like it is working and tells
you nothing.

Derivation is a guess, so it is never stored. `poi.label` holds an override for
where the guess is wrong, and the setup table shows the result of both so a
club can see two pins that came out the same and fix one.
"""

from __future__ import annotations

import re

# What fits in a pin. Raising this means changing .poi-label in app.css too,
# and re-checking it at the zoom where the map is actually read.
MAX_LEN = 2

# Words naming the KIND of place rather than which one it is. Stripped only
# when something else remains: a place genuinely called "Water" still gets "W".
_GENERIC = frozenset({
    "aid", "station", "stations", "stop", "stops", "post", "point", "spot",
    "water", "mile", "marker", "markers", "mm", "the", "at", "and",
    # Which race, not which station. A real organizer file is full of
    # "WATER (ALL)" and "Exchange Zone 3 (FULL)", and taking the first
    # non-generic word there labels a water stop "A" for ALL - confidently,
    # and with no relationship to the place.
    "all", "full", "half", "marathon", "10k", "5k", "relay",
})

_WORD = re.compile(r"[A-Za-z0-9]+")


def derive(name: str) -> str:
    """Guess a pin label from a place name. May be empty.

    Empty means "nothing useful to draw" - a pin with no label is honest,
    where a pin labelled with the wrong character is not.
    """
    words = _WORD.findall(name or "")
    if not words:
        return ""

    # A number in the name is almost always the identity: aid stations and
    # mile markers are numbered far more often than they are named, and "3"
    # is unambiguous where the first letter of "Aid 3" is not.
    for word in words:
        if word.isdigit():
            return word.lstrip("0")[:MAX_LEN] or word[:MAX_LEN]

    for word in words:
        if word.lower() not in _GENERIC:
            return word[0].upper()

    # Every word was generic - "Water Stop", say. Better its first letter than
    # nothing, because the club chose to label this layer.
    return words[0][0].upper()


def clean(override: str | None) -> str | None:
    """Normalise a typed override, or None if it is empty.

    Kept as typed apart from length and surrounding space: a club that writes
    "c" lowercase meant lowercase.
    """
    text = (override or "").strip()
    return text[:MAX_LEN] if text else None


def for_poi(name: str, override: str | None = None) -> str:
    """The label to actually draw: the override if there is one, else a guess."""
    return clean(override) or derive(name)
