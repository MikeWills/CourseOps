"""The one shape a stored timestamp has.

SQLite writes `strftime('%Y-%m-%dT%H:%M:%SZ','now')` into every `created_at`
and `received_at` default, and Python compares against those columns as
STRINGS - a session's `expires_at`, a link's `last_used` cutoff - so anything
Python writes or compares has to be the same shape to the character: no
microseconds, no `+00:00`, a literal `Z`. Three modules each had their own copy
of that format string and one of them had once drifted to a different one.
This is a leaf module on purpose: `parser.py` needs it and imports nothing
else from the app.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now_iso(offset: timedelta | None = None) -> str:
    """Now, in UTC, as SQLite stores it - optionally shifted by `offset`."""
    now = datetime.now(timezone.utc)
    if offset is not None:
        now = now + offset
    return now.strftime(TIMESTAMP_FORMAT)
