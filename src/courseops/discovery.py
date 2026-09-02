"""Pre-event check-in: find out which SSIDs your people are actually on the air with.

The problem this solves is silent and common. A volunteer signs up as
`WX0MIK-1`, but their phone app beacons `WX0MIK-5`. The roster says `-1`, the
APRS-IS filter asks for `-1`, and on race morning that person simply never
appears - no error, no warning, just an empty row.

The same callsign often has other SSIDs on the air that must NOT be tracked: a
digipeater at `-7`, an igate, a home weather station. So the fix cannot be
"widen the filter to `WX0MIK*`" - that would replace one problem with a map full
of fixed plant.

Instead this listens wide for a few minutes, reports every SSID heard under each
rostered base callsign, and says which look like people and which look like
infrastructure. A human then picks. Run it at a club meeting a week out, when
there is still time to fix things.
"""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from dataclasses import dataclass, field

from . import symbols
from .parser import PositionReport


def base_callsign(station_key: str) -> str:
    """`WX0MIK-5` -> `WX0MIK`. The part that identifies the operator."""
    return station_key.split("-", 1)[0].strip().upper()


def wildcard_filter(station_keys: list[str], per_clause: int = 20) -> str:
    """A budlist covering every SSID of each rostered callsign.

    Deliberately wider than the live filter. This is a discovery tool run before
    the event, not the race-day filter - the whole point is to see the SSIDs the
    roster does not know about yet.
    """
    bases = sorted({base_callsign(key) for key in station_keys if key.strip()})
    clauses = []
    for i in range(0, len(bases), per_clause):
        chunk = [f"{base}*" for base in bases[i:i + per_clause]]
        clauses.append("b/" + "/".join(chunk))
    return " ".join(clauses)


@dataclass
class Heard:
    """What we observed from one SSID."""

    station_key: str
    packets: int = 0
    positions: int = 0
    first_at: str | None = None
    last_at: str | None = None
    symbol_table: str | None = None
    symbol_code: str | None = None
    last_lat: float | None = None
    last_lon: float | None = None
    comments: list[str] = field(default_factory=list)

    @property
    def base(self) -> str:
        return base_callsign(self.station_key)

    @property
    def description(self) -> str:
        return symbols.describe(self.symbol_table, self.symbol_code)

    @property
    def looks_like_infrastructure(self) -> bool:
        return symbols.is_infrastructure(self.symbol_table, self.symbol_code)


class CheckIn:
    """Accumulates what was heard during a check-in listen."""

    def __init__(self, roster_keys: list[str]) -> None:
        self.roster = {key.strip().upper() for key in roster_keys if key.strip()}
        self.expected_bases = {base_callsign(key) for key in self.roster}
        self.heard: "OrderedDict[str, Heard]" = OrderedDict()

    def observe(self, report: PositionReport) -> None:
        entry = self.heard.get(report.station_key)
        if entry is None:
            entry = Heard(report.station_key, first_at=report.received_at)
            self.heard[report.station_key] = entry
        entry.packets += 1
        entry.positions += 1
        entry.last_at = report.received_at
        entry.symbol_table = report.symbol_table or entry.symbol_table
        entry.symbol_code = report.symbol_code or entry.symbol_code
        entry.last_lat, entry.last_lon = report.lat, report.lon
        if report.comment and report.comment not in entry.comments:
            entry.comments.append(report.comment[:60])

    # --- what the report is made of -----------------------------------------

    def confirmed(self) -> list[Heard]:
        """On the roster and heard. Nothing to do."""
        return [h for h in self.heard.values() if h.station_key in self.roster]

    def silent(self) -> list[str]:
        """On the roster and never heard.

        Either they were not transmitting during the listen, or the roster has
        the wrong SSID - check `mismatched()` before assuming the former.
        """
        return sorted(key for key in self.roster if key not in self.heard)

    def mismatched(self) -> dict[str, list[Heard]]:
        """The silent-but-actually-on-the-air case.

        Roster says `WX0MIK-1`, nothing heard from `-1`, but `WX0MIK-5` is
        beaconing. Almost always the roster has the wrong SSID.
        """
        result: dict[str, list[Heard]] = {}
        for rostered in self.silent():
            base = base_callsign(rostered)
            candidates = [
                h for h in self.heard.values()
                if h.base == base
                and h.station_key not in self.roster
                and not h.looks_like_infrastructure
            ]
            if candidates:
                result[rostered] = sorted(
                    candidates, key=lambda h: h.positions, reverse=True
                )
        return result

    def other_ssids(self) -> list[Heard]:
        """Heard under a rostered callsign but not on the roster.

        Usually the operator's own digipeater, igate or home station. Listed so
        it is a decision rather than a surprise - and so nobody "fixes" a
        missing person by pointing the roster at their digipeater.
        """
        return [
            h for h in self.heard.values()
            if h.station_key not in self.roster and h.base in self.expected_bases
        ]


def roster_keys_for_event(conn: sqlite3.Connection, event_id: int) -> list[str]:
    from . import db
    return db.all_station_keys(conn, event_id)
