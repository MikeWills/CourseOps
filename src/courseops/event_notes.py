"""Event notes: a sentence for the organizer, tied to nowhere in particular.

"Bring more pizza next year." "The start was twenty minutes late." "Van 2
needs a spare tyre." None of that is a pickup and none of it happened at a
point on the course, so none of it fits `incidents`, where a row is a pin
with a status. Forcing a location onto it would put a marker on the map that
means nothing and a coordinate on the report that lies.

So this is its own table and its own list: text, a time, and the operator's
annotation. No workflow, no position, no count anyone reads as "who is still
waiting". The time is always stored - the reader ignores it when it does not
matter, and "ran out of cups at 14:32" is a fact worth keeping when it does.

Anyone holding any link may add one (see `access.CAP_EVENT_NOTE`): every
volunteer's day is a different view of the event, and the note is what the
club has forgotten by the following spring.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db

# Longer than an incident note, which is capped to stay operational. This is
# after-action prose, not a runner's condition - there is no bib here and no
# person it could describe.
MAX_TEXT_LENGTH = 500
MAX_WHO_LENGTH = 24


class EventNoteError(ValueError):
    """Rejected input, or a note that is not this event's. Safe to show."""


@dataclass(frozen=True)
class EventNote:
    row: sqlite3.Row

    def as_dict(self) -> dict:
        return dict(self.row)


def _text(value: object) -> str:
    text = db.clean_text(value, MAX_TEXT_LENGTH)
    if not text:
        raise EventNoteError("A note needs some words.")
    return text


def get(conn: sqlite3.Connection, event_id: int, note_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM event_note WHERE id = ? AND event_id = ?",
        (note_id, event_id),
    ).fetchone()
    if row is None:
        raise EventNoteError(f"No event note {note_id} in this event.")
    return row


def create(
    conn: sqlite3.Connection, event_id: int, text: object, by: str | None = None
) -> sqlite3.Row:
    cur = conn.execute(
        "INSERT INTO event_note (event_id, text, created_by) VALUES (?, ?, ?)",
        (event_id, _text(text), db.clean_text(by, MAX_WHO_LENGTH)),
    )
    return get(conn, event_id, int(cur.lastrowid))


def update(
    conn: sqlite3.Connection, event_id: int, note_id: int, text: object
) -> sqlite3.Row:
    """Correct the words. The time and the annotation stay: it is the same
    note, said better, not a new one."""
    get(conn, event_id, note_id)          # raises if it is not ours
    conn.execute(
        "UPDATE event_note SET text = ? WHERE id = ? AND event_id = ?",
        (_text(text), note_id, event_id),
    )
    return get(conn, event_id, note_id)


def delete(conn: sqlite3.Connection, event_id: int, note_id: int) -> sqlite3.Row:
    """Remove one. Returns the row as it was, because every other browser
    holds it and has to be told which one to drop."""
    row = get(conn, event_id, note_id)
    conn.execute(
        "DELETE FROM event_note WHERE id = ? AND event_id = ?", (note_id, event_id)
    )
    return row


def for_event(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    """Newest first: the recent one is the one being discussed."""
    return conn.execute(
        "SELECT * FROM event_note WHERE event_id = ? ORDER BY created_at DESC, id DESC",
        (event_id,),
    ).fetchall()
