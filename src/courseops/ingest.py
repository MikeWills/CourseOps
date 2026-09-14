"""Feed -> parse -> store. The seam the WebSocket fan-out plugs into later."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from . import aprsis, db, symbols
from .config import ConfigError, Settings
from .parser import PositionReport, Rejected, parse_packet

log = logging.getLogger(__name__)


class IngestError(RuntimeError):
    """The feed cannot start or cannot go on, and this is why.

    A RuntimeError and never SystemExit. `run_ingest` runs inside a task on
    the server's event loop, and asyncio re-raises SystemExit out of a task
    and out of the loop itself: one event that could not start - no roster,
    no course, a callsign lost between deploys - took the whole site down,
    every role page with it, and the persisted switch restarted it into the
    same crash under systemd. The CLI is the only place this becomes an exit
    code, and it does the translation itself.
    """


# What the tracking switch and the feed both say about an event with nothing
# to listen for. One string, because the switch refuses the case up front and
# the feed refuses it again if reached some other way, and the two must agree.
NOTHING_TO_LISTEN_FOR = (
    "This event has no stations expected to beacon, no course and no extra "
    "filter. Add stations or import a course before turning tracking on."
)

# Called for each stored position. Phase 3 hangs the WebSocket broadcast here.
PositionHandler = Callable[[int, PositionReport], Awaitable[None]]


@dataclass
class IngestStats:
    stored: int = 0
    not_rostered: int = 0
    excluded: int = 0
    no_position: int = 0
    parse_errors: int = 0
    by_station: dict[str, int] = field(default_factory=dict)
    # SSIDs kept because their callsign is rostered, though the roster names a
    # different SSID. Almost always a signup typo worth telling the operator
    # about rather than silently absorbing.
    unexpected_ssid: set[str] = field(default_factory=set)
    # Bare-callsign roster entries that learned their SSID this run:
    # heard key -> the label it was attached to.
    bound: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        summary = (
            f"stored={self.stored} not_rostered={self.not_rostered} "
            f"excluded={self.excluded} no_position={self.no_position} "
            f"parse_errors={self.parse_errors} stations={len(self.by_station)}"
        )
        if self.unexpected_ssid:
            summary += f" unexpected_ssid={sorted(self.unexpected_ssid)}"
        if self.bound:
            summary += f" bound={sorted(self.bound)}"
        return summary


def handle_line(
    conn: sqlite3.Connection,
    event_id: int,
    roster_keys: set[str],
    line: str,
    stats: IngestStats,
    log_all_raw: bool = True,
    base_callsigns: set[str] | None = None,
    excluded: set[str] | None = None,
    nearby: list[PositionReport] | None = None,
) -> PositionReport | None:
    """Parse and store one line. Returns the report if it was stored.

    A report from a station the roster does not know is NOT stored and not
    logged - not even raw. The area filter delivers the public, and the deal
    is that they are seen, in memory, by NCS, and written down only once NCS
    says who they are. It goes into `nearby` for the caller to hand on.
    """
    try:
        report = parse_packet(line)
    except Rejected as rejection:
        if rejection.reason == "parse_error":
            stats.parse_errors += 1
            log.debug("Parse error: %s | %s", rejection.detail, line)
        else:
            stats.no_position += 1
        if log_all_raw:
            db.log_raw_packet(
                conn, event_id, _now(), line, rejection.reason, rejection.detail
            )
        return None

    # A station NCS has ignored. Dropped here, at the door, and not written
    # anywhere - not even raw. Ignoring is meant to be the end of it until
    # somebody unignores; the loop re-reads membership before every packet
    # (rate-limited), so it holds within MEMBERSHIP_REFRESH_S and needs no
    # reconnect.
    if excluded and report.station_key in excluded:
        stats.excluded += 1
        return None

    # Accept any SSID of a rostered callsign, not just the exact one on the
    # roster. Without this the wildcard filter would be pointless: WX0MIK-5
    # would arrive and then be thrown away because the roster says -1.
    known = report.station_key in roster_keys
    if not known and base_callsigns:
        known = report.station_key.split("-", 1)[0] in base_callsigns
        if known:
            stats.unexpected_ssid.add(report.station_key)

    # The roster is the allowlist. With no roster at all nothing is known and
    # nothing is stored, which is what makes an area filter safe to run.
    if not known:
        stats.not_rostered += 1
        if nearby is not None:
            nearby.append(report)
        return None

    db.insert_position(conn, event_id, report)

    # A roster entry naming a bare callsign is waiting to learn its SSID.
    # Infrastructure is skipped deliberately: the wildcard filter drags in the
    # operator's own digipeater or igate, and binding an aid station to their
    # home igate would park that person on the map at their house all day -
    # confidently, and wrongly.
    if not symbols.is_infrastructure(report.symbol_table, report.symbol_code):
        bound = db.bind_heard_ssid(conn, event_id, report.station_key)
        if bound is not None:
            stats.bound[report.station_key] = bound["display_label"]

    if log_all_raw:
        db.log_raw_packet(conn, event_id, report.received_at, line, "stored")
    stats.stored += 1
    stats.by_station[report.station_key] = stats.by_station.get(report.station_key, 0) + 1
    return report


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# How far beyond the course's own extent the area filter reaches. A mile is
# enough to catch someone parked at a trailhead or driving to their station
# without pulling in the next town over.
AREA_MARGIN_M = 1609.344

# How often the ingest loop re-reads who is on the roster. NCS binds a station
# heard nearby to a roster entry mid-event, and from then on its packets have
# to be STORED rather than held in memory; NCS ignores a digipeater and from
# then on its packets must be dropped - so membership cannot be a snapshot
# taken when the feed started. Re-read before each packet, at most this often.
MEMBERSHIP_REFRESH_S = 5.0


class Membership:
    """Who the roster knows, re-read cheaply while the feed runs."""

    def __init__(self, conn: sqlite3.Connection, event_id: int) -> None:
        self._conn = conn
        self._event_id = event_id
        self._read_at = 0.0
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        import time
        now = time.monotonic()
        if not force and now - self._read_at < MEMBERSHIP_REFRESH_S:
            return
        self._read_at = now
        conn, event_id = self._conn, self._event_id
        self.roster_keys = set(db.all_station_keys(conn, event_id))
        self.roster_keys |= db.bound_station_keys(conn, event_id)
        self.base_callsigns = db.rostered_base_callsigns(conn, event_id)
        self.excluded = db.excluded_station_keys(conn, event_id)


async def run_ingest(
    settings: Settings,
    event_slug: str,
    on_position: PositionHandler | None = None,
    max_packets: int | None = None,
    on_nearby: PositionHandler | None = None,
) -> IngestStats:
    """Connect to APRS-IS for one event and store what arrives.

    Requires a callsign, and says so here rather than at startup: everything
    else in the app works without one, and refusing to boot over it is what
    made a first run on Windows a console window that flashed and vanished.

    `max_packets` bounds the run for smoke-testing; None runs until cancelled.
    """
    try:
        settings.require_callsign()
    except ConfigError as exc:
        raise IngestError(str(exc)) from exc
    conn = db.connect(settings.db_path)
    db.init_schema(conn)

    event = db.get_event(conn, event_slug)
    if event is None:
        conn.close()
        raise IngestError(f"No event with slug {event_slug!r}. Create it first.")

    # The filter asks only for stations we expect to beacon; the membership
    # check accepts anyone on the roster, since an area filter can legitimately
    # deliver a rostered operator who was not expected to report.
    filter_keys = db.tracked_station_keys(conn, event["id"])
    membership = Membership(conn, event["id"])
    from . import progress
    area = progress.CourseIndex.for_event(conn, event["id"]).area(AREA_MARGIN_M)
    if not filter_keys and not event["aprs_filter_extra"] and area is None:
        conn.close()
        raise IngestError(NOTHING_TO_LISTEN_FOR)

    aprs_filter = aprsis.build_filter(
        filter_keys, event["aprs_filter_extra"],
        area=(area[0], area[1], area[2] / 1000.0) if area else None,
    )
    log.info(
        "Ingesting event %r (%d rostered, %d expected to beacon, %d excluded)",
        event_slug, len(membership.roster_keys), len(filter_keys),
        len(membership.excluded),
    )

    stats = IngestStats()
    seen = 0
    try:
        async for line in aprsis.stream_packets(
            settings.host, settings.port, settings.callsign,
            settings.passcode, aprs_filter,
        ):
            # Re-read who the roster knows BEFORE the packet is judged, not
            # after. This used to happen only once an unknown station was
            # heard, which is the wrong trigger twice over: an ignored SSID
            # under a rostered callsign is never unknown, so Ignore did not
            # reach the door until some stranger beaconed - on a quiet band,
            # not for a long time, and the igate NCS had just dismissed kept
            # coming back on every screen. And the packet that revealed a
            # match had already been thrown away by the time the re-read
            # showed it was wanted. Rate-limited inside refresh(), so this
            # costs nothing per packet most of the time.
            membership.refresh()
            nearby: list[PositionReport] = []
            report = handle_line(
                conn, event["id"], membership.roster_keys, line, stats,
                base_callsigns=membership.base_callsigns,
                excluded=membership.excluded, nearby=nearby,
            )
            if nearby and on_nearby is not None:
                await on_nearby(event["id"], nearby[0])
            if report is not None:
                log.info(
                    "%s  %.5f,%.5f  %s",
                    report.station_key, report.lat, report.lon, report.aprs_format,
                )
                if on_position is not None:
                    await on_position(event["id"], report)

            seen += 1
            if max_packets is not None and seen >= max_packets:
                break
    finally:
        log.info("Ingest stopped: %s", stats.summary())
        conn.close()

    return stats
