"""Phone tracking: positions posted by a tracking app on a non-ham's phone.

Bike medics, race staff and non-licensed drivers have no callsign, and APRS
is the only other tracking mechanism. A web page cannot track a phone in the
background - iOS suspends it the moment the phone is pocketed, silently - so
the reliable route is a dedicated app with real background-location
permission (OwnTracks, Traccar Client - both free and open source) posting
to us. This module receives what they send.

One URL per event, one printed QR code, and each person types their own
designator (M1, BIKE 2) into the app. The roster is the allowlist: a report
under a designator the roster does not know is held in memory for NCS to
match, exactly as an unknown APRS station is, and never written down until
someone says who it is. Design and the reasoning are in
docs/phone-tracking.md; the decision record is issue #6.

Two payload shapes, because they are cheap to accept and let a club use
whichever app people already have:

* OsmAnd protocol (Traccar Client): query or form parameters -
  ``id``/``deviceid``, ``lat``, ``lon``, ``timestamp``, ``speed``,
  ``bearing``, ``altitude``, ``accuracy``, ``batt``.
* OwnTracks HTTP mode: a JSON ``{"_type": "location", ...}`` with ``tid``
  (the designator), ``lat``, ``lon``, ``tst`` (epoch seconds), ``vel``
  (km/h), ``cog``, ``alt``, ``acc``, ``batt``.

Nothing here is a parser in the aprslib sense - the shapes are flat and
documented - but the same rule applies as for the APRS feed: what is
stored is metric, and the reported timestamp is what is stored, never the
arrival time (see `reported_at`).
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

import segno

from . import db
from .clock import TIMESTAMP_FORMAT
from .parser import PositionReport

# What a stored phone position says it is. `aprs_format` names the source so
# a row dump can tell a phone fix from a packet; the symbol pair is the APRS
# "phone" symbol, so everything that describes a station by its symbol says
# something true, and `symbols.is_infrastructure` stays False - a phone is a
# person, and may be bound to a roster entry.
FORMAT = "phone"
SYMBOL_TABLE = "/"
SYMBOL_CODE = "$"

# The longest designator accepted, after normalisation. OwnTracks' `tid` is
# conventionally two characters and Traccar's identifier is free text; this
# is generous for either and short enough to be said on the air.
DESIGNATOR_MAX = 10

# The longest raw payload kept alongside a stored position. A row keeps
# what it was built from, as an APRS row keeps its packet, but an app is
# free to send anything and the column is not a log.
RAW_MAX = 500

_SEPARATORS = re.compile(r"[-_\s]+")


class Rejected(ValueError):
    """A report that cannot be stored, with the reason. Never surfaced to the
    app - it gets 200 regardless - but recorded in the stats."""


def normalise_designator(text: object) -> str:
    """The one spelling of a designator, on both sides of the match.

    `Medic1`, `medic 1`, `Medic-1` and `MEDIC_1` are four strings and one
    person, and a mismatch makes that person invisible while their phone
    transmits happily - the wrong-SSID failure this app already has scar
    tissue for. So: upper-case, and spaces, `_` and `-` are dropped
    altogether (the KML hint patterns needed the same family of rule, for
    the same reason). The roster's own keys are stored in this spelling,
    so "M-1" typed into setup is M1 on the card, and `m 1` on a phone
    matches it.
    """
    if text is None:
        return ""
    cleaned = _SEPARATORS.sub("", str(text)).upper()
    return cleaned[:DESIGNATOR_MAX]


def _float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def reported_at(value: object, now: datetime | None = None) -> str:
    """The fix's own timestamp, in the one stored shape.

    Both apps buffer while out of coverage and deliver the backlog when it
    returns, so a burst arriving now is a set of real positions from
    earlier. Staleness, the map and "newest position" all key off this
    value, never the arrival time - or a medic returning to coverage looks
    freshly located at a place they left ten minutes ago.

    Accepts epoch seconds or milliseconds (Traccar sends either; OwnTracks
    sends seconds), ISO 8601 with or without a zone, and Traccar's
    `yyyy-MM-dd HH:mm:ss`. A missing or unreadable timestamp falls back to
    now, which is the honest choice for a fix the app says is current. A
    timestamp in the future is clamped to now: a phone with a wrong clock
    must not produce a position that stays "fresh" for an hour.
    """
    now = now or datetime.now(timezone.utc)
    stamp: datetime | None = None
    number = _float(value)
    if number is not None and number > 0:
        # Milliseconds if it is far too large to be seconds. 10^11 s is the
        # year 5138, so anything above it is ms.
        if number > 1e11:
            number /= 1000.0
        try:
            stamp = datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            stamp = None
    elif value:
        text = str(value).strip().replace(" ", "T", 1)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            stamp = None
        if stamp is not None and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
    if stamp is None or stamp > now:
        stamp = now
    return stamp.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def _coordinates(lat: object, lon: object) -> tuple[float, float]:
    latitude, longitude = _float(lat), _float(lon)
    if latitude is None or longitude is None:
        raise Rejected("no position")
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise Rejected("position out of range")
    if latitude == 0 and longitude == 0:
        # Null Island is what a GPS that has not locked reports, and a pin
        # there is a position nobody was ever at.
        raise Rejected("position out of range")
    return latitude, longitude


def _comment(accuracy: object, battery: object) -> str | None:
    """A short, human-readable note of what the app said about the fix.

    Shown in the marker popup where an APRS comment would be. Accuracy is
    the one that matters: a 400 m fix from wifi triangulation must not read
    as GPS, the same rule the "Here" button follows.
    """
    bits = []
    acc, batt = _float(accuracy), _float(battery)
    if acc is not None:
        bits.append(f"accuracy {acc:.0f} m")
    if batt is not None:
        bits.append(f"battery {batt:.0f}%")
    return ", ".join(bits) or None


def parse_osmand(params: Mapping[str, Any], raw: str = "",
                 now: datetime | None = None) -> tuple[str, PositionReport]:
    """One Traccar Client (OsmAnd protocol) report: (designator, report).

    The designator comes back separately because the report's station_key
    is decided by the match, not by the app - the roster row it lands on
    may carry a bound key.
    """
    designator = normalise_designator(params.get("id") or params.get("deviceid"))
    if not designator:
        raise Rejected("no designator")
    lat, lon = _coordinates(params.get("lat"), params.get("lon"))
    speed = _float(params.get("speed"))
    # Traccar sends speed in knots (the OsmAnd protocol's convention); store
    # km/h like every other row.
    speed_kmh = speed * 1.852 if speed is not None else None
    report = PositionReport(
        station_key=designator,
        received_at=reported_at(params.get("timestamp"), now),
        lat=lat, lon=lon,
        course_deg=_float(params.get("bearing")),
        speed_kmh=speed_kmh,
        altitude_m=_float(params.get("altitude")),
        symbol_table=SYMBOL_TABLE, symbol_code=SYMBOL_CODE,
        comment=_comment(params.get("accuracy"), params.get("batt")),
        aprs_format=FORMAT,
        raw=(raw or "")[:RAW_MAX],
    )
    return designator, report


def parse_owntracks(body: bytes | str, now: datetime | None = None
                    ) -> tuple[str, PositionReport] | None:
    """One OwnTracks HTTP-mode message: (designator, report).

    Returns None for a message that is not a location - OwnTracks also
    posts `_type: lwt`, `transition`, `waypoint` and others, and they are
    acknowledged and ignored rather than rejected, because an app that
    gets an error for a waypoint may stop sending locations too.
    """
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    try:
        message = json.loads(text)
    except ValueError:
        raise Rejected("not JSON") from None
    if not isinstance(message, dict):
        raise Rejected("not a message")
    if message.get("_type") != "location":
        return None
    # `tid` is the tracker id and is required in HTTP mode. `topic` is the
    # MQTT topic the app would have published to (owntracks/user/device);
    # its last segment is the device name, and is the fallback for an app
    # whose tid was left at the default.
    designator = normalise_designator(message.get("tid"))
    if not designator:
        topic = str(message.get("topic") or "")
        designator = normalise_designator(topic.rsplit("/", 1)[-1])
    if not designator:
        raise Rejected("no designator")
    lat, lon = _coordinates(message.get("lat"), message.get("lon"))
    report = PositionReport(
        station_key=designator,
        received_at=reported_at(message.get("tst"), now),
        lat=lat, lon=lon,
        course_deg=_float(message.get("cog")),
        speed_kmh=_float(message.get("vel")),
        altitude_m=_float(message.get("alt")),
        symbol_table=SYMBOL_TABLE, symbol_code=SYMBOL_CODE,
        comment=_comment(message.get("acc"), message.get("batt")),
        aprs_format=FORMAT,
        raw=text[:RAW_MAX],
    )
    return designator, report


def match_roster(conn: sqlite3.Connection, event_id: int,
                 designator: str) -> sqlite3.Row | None:
    """The roster entry a designator belongs to, or None.

    Matches the entry's own key, normalised the same way, or a key NCS
    bound to it on the live map - which is how a designator typed wrongly
    on the phone ("MEDIC1" for M1) is attributed without anyone touching
    the phone. Any entry may match, not only phone-tracked ones: a ham who
    installs the app on race morning is still one of ours.
    """
    wanted = normalise_designator(designator)
    if not wanted:
        return None
    for row in conn.execute(
        "SELECT * FROM roster WHERE event_id = ?", (event_id,)
    ).fetchall():
        if normalise_designator(row["station_key"]) == wanted:
            return row
        if row["bound_key"] and normalise_designator(row["bound_key"]) == wanted:
            return row
    return None


def store(conn: sqlite3.Connection, event_id: int,
          report: PositionReport) -> PositionReport | None:
    """Store one matched report under the key it will be joined on.

    Returns the report as stored - its station_key is the roster entry's
    tracking key, so a bound designator lands where the map looks for it -
    or None for a duplicate. Both apps resend a buffered fix when the
    acknowledgement was lost, and the same station at the same second is
    the same fix.
    """
    exists = conn.execute(
        "SELECT 1 FROM position WHERE event_id = ? AND station_key = ?"
        " AND received_at = ? AND aprs_format = ? LIMIT 1",
        (event_id, report.station_key, report.received_at, FORMAT),
    ).fetchone()
    if exists:
        return None
    db.insert_position(conn, event_id, report)
    return report


def attribute(report: PositionReport, entry: sqlite3.Row) -> PositionReport:
    """The report re-keyed to the roster entry it matched."""
    key = db.tracking_key(entry)
    if key == report.station_key:
        return report
    return PositionReport(**{**report.__dict__, "station_key": key})


def is_newest(conn: sqlite3.Connection, event_id: int,
              report: PositionReport) -> bool:
    """Whether this fix is newer than everything stored for its station.

    A buffered backlog arrives in whatever order the app kept it. Every fix
    is stored - the track is real - but only a fix newer than the newest
    already held is worth publishing, or the marker would walk backwards
    through the backlog and stop wherever the burst happened to end.
    """
    row = conn.execute(
        "SELECT MAX(received_at) AS newest FROM position"
        " WHERE event_id = ? AND station_key = ?",
        (event_id, report.station_key),
    ).fetchone()
    return row is None or row["newest"] is None or report.received_at > row["newest"]


def owntracks_config(url: str) -> dict[str, Any]:
    """The `.otrc` configuration the event's QR code carries.

    Only the endpoint and the mode: the person sets their own `tid` in
    the app, which is the whole point of one code for everybody. `monitoring`
    is "move" - OwnTracks' default is "significant changes", which on iOS is
    a report every few hundred metres or several minutes, and a medic sent
    to a runner down needs better than that.
    """
    return {
        "_type": "configuration",
        "mode": 3,          # HTTP
        "url": url,
        "monitoring": 2,    # move: report at locatorInterval / locatorDisplacement
        "locatorInterval": 60,
        "locatorDisplacement": 25,
    }


def owntracks_link(url: str) -> str:
    """The `owntracks:///config?inline=` link the QR code encodes.

    Documented in the OwnTracks booklet as a supported way to configure the
    app on both platforms - a public mechanism, which is what a club needs
    to still work next year. Traccar Client's equivalent deep link is an
    implementation detail with unresolved iOS reports against it, so it is
    not what a club is told to scan; its users type the URL instead.
    """
    config = json.dumps(owntracks_config(url), separators=(",", ":"))
    encoded = base64.b64encode(config.encode("utf-8")).decode("ascii")
    return f"owntracks:///config?inline={encoded}"


def qr_svg(data: str) -> str:
    """An inline SVG of `data` as a QR code, sized by the page's CSS.

    Error level M: a printed card gets creased and a phone camera at the
    briefing table is not a scanner. Rendered by the server so the page
    ships no encoder of its own. `omitsize` puts a viewBox on it instead of
    a fixed width, so the CSS sizes it - without one the box scales and the
    drawing stays 70px.
    """
    return segno.make(data, error="m").svg_inline(scale=1, border=2, omitsize=True)
