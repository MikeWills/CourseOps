"""Feed -> parse -> store. The seam the WebSocket fan-out plugs into later."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from . import aprsis, db
from .config import Settings
from .parser import PositionReport, Rejected, parse_packet

log = logging.getLogger(__name__)

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

    def summary(self) -> str:
        summary = (
            f"stored={self.stored} not_rostered={self.not_rostered} "
            f"excluded={self.excluded} no_position={self.no_position} "
            f"parse_errors={self.parse_errors} stations={len(self.by_station)}"
        )
        if self.unexpected_ssid:
            summary += f" unexpected_ssid={sorted(self.unexpected_ssid)}"
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
) -> PositionReport | None:
    """Parse and store one line. Returns the report if it was stored."""
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

    # An SSID explicitly dismissed before the event - the operator's own
    # digipeater or igate, which the wildcard filter drags in.
    if excluded and report.station_key in excluded:
        stats.excluded += 1
        if log_all_raw:
            db.log_raw_packet(conn, event_id, report.received_at, line, "excluded")
        return None

    # Accept any SSID of a rostered callsign, not just the exact one on the
    # roster. Without this the wildcard filter would be pointless: WX0MIK-5
    # would arrive and then be thrown away because the roster says -1.
    known = report.station_key in roster_keys
    if not known and base_callsigns:
        known = report.station_key.split("-", 1)[0] in base_callsigns
        if known:
            stats.unexpected_ssid.add(report.station_key)

    if (roster_keys or base_callsigns) and not known:
        stats.not_rostered += 1
        if log_all_raw:
            db.log_raw_packet(conn, event_id, report.received_at, line, "not_rostered")
        return None

    db.insert_position(conn, event_id, report)
    if log_all_raw:
        db.log_raw_packet(conn, event_id, report.received_at, line, "stored")
    stats.stored += 1
    stats.by_station[report.station_key] = stats.by_station.get(report.station_key, 0) + 1
    return report


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def run_ingest(
    settings: Settings,
    event_slug: str,
    on_position: PositionHandler | None = None,
    max_packets: int | None = None,
) -> IngestStats:
    """Connect to APRS-IS for one event and store what arrives.

    `max_packets` bounds the run for smoke-testing; None runs until cancelled.
    """
    conn = db.connect(settings.db_path)
    db.init_schema(conn)

    event = db.get_event(conn, event_slug)
    if event is None:
        raise SystemExit(f"No event with slug {event_slug!r}. Create it first.")

    # The filter asks only for stations we expect to beacon; the membership
    # check accepts anyone on the roster, since an area filter can legitimately
    # deliver a rostered operator who was not expected to report.
    filter_keys = db.tracked_station_keys(conn, event["id"])
    roster_keys = set(db.all_station_keys(conn, event["id"]))
    base_callsigns = db.rostered_base_callsigns(conn, event["id"])
    excluded = db.excluded_station_keys(conn, event["id"])
    if not filter_keys and not event["aprs_filter_extra"]:
        raise SystemExit(
            f"Event {event_slug!r} has no APRS-expecting roster entries and no "
            "extra filter. Add stations, or an area filter, before ingesting."
        )

    aprs_filter = aprsis.build_filter(filter_keys, event["aprs_filter_extra"])
    log.info(
        "Ingesting event %r (%d rostered, %d expected to beacon, %d excluded)",
        event_slug, len(roster_keys), len(filter_keys), len(excluded),
    )

    stats = IngestStats()
    seen = 0
    try:
        async for line in aprsis.stream_packets(
            settings.host, settings.port, settings.callsign,
            settings.passcode, aprs_filter,
        ):
            report = handle_line(
                conn, event["id"], roster_keys, line, stats,
                base_callsigns=base_callsigns, excluded=excluded,
            )
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
