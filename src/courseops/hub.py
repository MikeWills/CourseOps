"""In-process fan-out from the APRS-IS ingest to connected browsers.

One APRS-IS connection feeds the whole server; browsers subscribe here. This is
deliberately in-process — no Redis, no broker. At under 50 stations and a
handful of viewers per event, a broker would be one more thing for a club to
install and nothing more.

Each subscriber gets a bounded queue. If a browser stalls (a phone that slept,
a dead cell zone), its queue fills and further messages for THAT subscriber are
dropped rather than allowed to back up and stall the ingest loop. A dropped
update is harmless: the client resyncs the full state on reconnect, and the
next position report supersedes the lost one anyway.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Enough to ride out a brief stall, small enough that a wedged client cannot
# consume meaningful memory.
QUEUE_MAXSIZE = 64


# eq=False keeps identity hashing: subscriptions live in a set, and two
# browsers on the same event are distinct subscribers even though their fields
# are identical. Value equality here would silently collapse them into one.
@dataclass(eq=False)
class Subscription:
    event_id: int
    queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    )
    dropped: int = 0


class Hub:
    """Per-event publish/subscribe over asyncio queues."""

    def __init__(self) -> None:
        self._subscribers: dict[int, set[Subscription]] = defaultdict(set)

    def subscribe(self, event_id: int) -> Subscription:
        sub = Subscription(event_id=event_id)
        self._subscribers[event_id].add(sub)
        log.debug("Subscriber added for event %s (%d total)",
                  event_id, len(self._subscribers[event_id]))
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subscribers.get(sub.event_id, set()).discard(sub)

    def subscriber_count(self, event_id: int) -> int:
        return len(self._subscribers.get(event_id, ()))

    async def publish(self, event_id: int, message: dict[str, Any]) -> None:
        """Fan a message out. Never blocks on a slow subscriber."""
        for sub in list(self._subscribers.get(event_id, ())):
            try:
                sub.queue.put_nowait(message)
            except asyncio.QueueFull:
                sub.dropped += 1
                if sub.dropped in (1, 10, 100):
                    log.warning(
                        "Subscriber on event %s is not keeping up (%d dropped)",
                        event_id, sub.dropped,
                    )


def position_message(report, roster_row=None, course_position=None) -> dict[str, Any]:
    """Wire format for one live position.

    Speed stays metric on the wire; the browser converts for display, keeping
    the storage/presentation split intact all the way out to the client.
    """
    message = {
        "type": "position",
        "station_key": report.station_key,
        "received_at": report.received_at,
        "lat": report.lat,
        "lon": report.lon,
        "course_deg": report.course_deg,
        "speed_kmh": report.speed_kmh,
        "altitude_m": report.altitude_m,
        "symbol_table": report.symbol_table,
        "symbol_code": report.symbol_code,
        "comment": report.comment,
    }
    if roster_row is not None:
        message["label"] = roster_row["display_label"]
        message["category"] = roster_row["category"]
    # None means "not near any course" - the client shows nothing rather than a
    # plausible wrong mile figure.
    message["course_position"] = course_position
    return message
