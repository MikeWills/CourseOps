"""Raw APRS packet text -> a normalized position report.

Parsing is delegated to aprslib, which handles the four mutually incompatible
position encodings (uncompressed, base-91 compressed, Mic-E, NMEA). Mic-E in
particular hides latitude in the AX.25 destination field; hand-rolling it is
the single largest avoidable time sink in this project.

aprslib normalizes units for us: speed in km/h, altitude in meters, course in
degrees true.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import aprslib


class Rejected(Exception):
    """Packet is not a usable position report. `reason` maps to raw_packet.status."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PositionReport:
    station_key: str          # SSID-qualified, e.g. 'N0CALL-9'
    received_at: str          # ISO-8601 UTC, when we saw it
    lat: float
    lon: float
    course_deg: float | None
    speed_kmh: float | None
    altitude_m: float | None
    symbol_table: str | None  # table and code are meaningless apart; keep paired
    symbol_code: str | None
    comment: str | None
    aprs_format: str | None
    raw: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drop NaN


def parse_packet(raw: str, received_at: str | None = None) -> PositionReport:
    """Parse one APRS-IS line. Raises Rejected for anything not a position."""
    raw = raw.strip()
    if not raw:
        raise Rejected("parse_error", "empty line")

    try:
        packet = aprslib.parse(raw)
    except (aprslib.ParseError, aprslib.UnknownFormat) as exc:
        raise Rejected("parse_error", str(exc)) from exc
    except Exception as exc:  # aprslib raises bare exceptions on malformed input
        raise Rejected("parse_error", f"{type(exc).__name__}: {exc}") from exc

    lat = _number(packet.get("latitude"))
    lon = _number(packet.get("longitude"))
    if lat is None or lon is None:
        # Status, messages, telemetry-only, weather without position, etc.
        raise Rejected("no_position", packet.get("format", "unknown"))
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise Rejected("parse_error", f"out of range: {lat},{lon}")
    if lat == 0.0 and lon == 0.0:
        # Null Island: a tracker with no GPS fix, not a real position.
        raise Rejected("no_position", "null island")

    station_key = _clean(packet.get("from"))
    if not station_key:
        raise Rejected("parse_error", "missing source callsign")

    return PositionReport(
        station_key=station_key.upper(),
        received_at=received_at or _utc_now_iso(),
        lat=lat,
        lon=lon,
        course_deg=_number(packet.get("course")),
        speed_kmh=_number(packet.get("speed")),
        altitude_m=_number(packet.get("altitude")),
        symbol_table=_clean(packet.get("symbol_table")),
        symbol_code=_clean(packet.get("symbol")),
        comment=_clean(packet.get("comment")),
        aprs_format=_clean(packet.get("format")),
        raw=raw,
    )
