"""What3Words addresses for aid stations.

Entered by hand and stored as text. We deliberately do NOT call the What3Words
API: it is a paid service, and requiring a key would add a signup and a billing
relationship to club setup for a field that changes once per event.

That means we cannot verify an address is real or resolve it to coordinates.
Validation here is shape-only, and it is advisory: a typo that still looks like
three words will be stored and shown. The lat/lon from KML remains the
authoritative location; the W3W string is a convenience for voice traffic.
"""

from __future__ import annotations

import re

# Three words, dot-separated. Leading '///' is how W3W writes them publicly and
# is accepted but not stored. Letters only, allowing non-ASCII for other locales.
_W3W_RE = re.compile(r"^/{0,3}([^\W\d_]+)\.([^\W\d_]+)\.([^\W\d_]+)$", re.UNICODE)


def normalize(value: str | None) -> str | None:
    """Strip decoration and lowercase. Returns None for blank input."""
    if value is None:
        return None
    text = value.strip().lstrip("/").strip().lower()
    return text or None


def is_plausible(value: str | None) -> bool:
    """Shape check only — three dot-separated words. Never proves it is real."""
    text = normalize(value)
    return bool(text and _W3W_RE.match(text))


def format_for_display(value: str | None) -> str:
    """Render with the leading /// that hams and dispatchers recognize."""
    text = normalize(value)
    return f"///{text}" if text else "--"
