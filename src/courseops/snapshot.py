"""The state snapshot and the live-feed callbacks.

`build_state` is everything a client needs to draw the map from scratch; the
two handlers are what the ingest task calls per packet to fan a position out
or hold an unknown station for NCS. None of it is HTTP: this is what the
routes and the feed both read, kept apart from either so it can be exercised
without building the application.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import (access, categories, db, event_notes, hub as hub_module, incidents,
               labels as poi_labels, leaders, progress, symbols)

# How long without a packet before a station is styled as going stale / silent.
# Phone apps beacon every 1-5 minutes, so "quiet for 4 minutes" is normal and
# must not read as an alarm.
STALE_AFTER_SECONDS = 10 * 60
SILENT_AFTER_SECONDS = 20 * 60


def course_position(index: "progress.CourseIndex", lat: float, lon: float):
    located = index.locate(lat, lon)
    return located.as_dict() if located else None


def position_message_for(report, index) -> dict[str, Any]:
    """One live position, framed for the socket, with its course position."""
    return hub_module.position_message(
        report, course_position(index, report.lat, report.lon))


def make_position_handler(hub, known_keys: set[str], index):
    """The ingest callback: fan a position out, and announce a new station.

    The SSID alerts ("Needs attention") are computed from stored positions
    and sent with the state snapshot, so a station the roster does not name
    used to reach every browser as a marker while the alert about it waited
    for someone to refresh. That defeats the alert: a wrong SSID is a silent
    failure, and the whole point is to put it in front of NCS unprompted.

    So the first packet from a station the roster does not know triggers one
    resync, which reloads the snapshot and with it the alerts. Once per
    station for the life of the feed - not per packet, which would have every
    browser reloading on every beacon from an igate.
    """
    announced: set[str] = set()

    async def on_position(event_id: int, report) -> None:
        await hub.publish(event_id, position_message_for(report, index))
        key = report.station_key
        if key not in known_keys and key not in announced:
            announced.add(key)
            await hub.publish(event_id, {"type": "resync"})

    return on_position


# The most stations held in memory per event. The area filter can deliver a
# whole town's worth on a busy band; beyond this the oldest is dropped, and
# it will be heard again if it is still there.
NEARBY_MAX = 200


def make_nearby_handler(hub, store: dict, index):
    """Stations heard near the course that the roster does not know.

    Held in memory only and sent only to a role that can act on them. This is
    the deal that makes an area filter acceptable: the public is SEEN by NCS,
    so a borrowed rig or a club tracker can be matched to the person using
    it, and nothing about anyone is written down until NCS says who they are.
    A restart empties the list, which costs one beacon interval.
    """
    async def on_nearby(event_id: int, report) -> None:
        entries = store.setdefault(event_id, {})
        previous = entries.get(report.station_key)
        located = index.locate(report.lat, report.lon)
        entry = {
            "station_key": report.station_key,
            "received_at": report.received_at,
            "lat": report.lat,
            "lon": report.lon,
            "symbol": symbols.describe(report.symbol_table, report.symbol_code),
            "looks_like_infrastructure": symbols.is_infrastructure(
                report.symbol_table, report.symbol_code),
            # "phone" for a tracking app posting under a designator the
            # roster does not know - almost always a designator typed
            # differently from the card, which the client says outright.
            "source": "phone" if report.aprs_format == "phone" else "aprs",
            "packets": (previous["packets"] + 1) if previous else 1,
            "course_position": located.as_dict() if located else None,
        }
        entries[report.station_key] = entry
        if len(entries) > NEARBY_MAX:
            oldest = min(entries.values(), key=lambda e: e["received_at"])
            entries.pop(oldest["station_key"], None)
        await hub.publish(event_id, {"type": "nearby", **entry},
                          requires=access.CAP_SSID)

    return on_nearby


def ssid_alerts(conn: sqlite3.Connection, event_id: int) -> list[dict[str, Any]]:
    """Callsigns transmitting on an SSID the roster does not name.

    Each alert carries the roster entries for the same callsign, so the client
    can offer "this is really Aid 3" without a second round trip.
    """
    alerts = []
    for row in db.unexpected_ssids(conn, event_id):
        base = row["station_key"].split("-", 1)[0]
        candidates = db.roster_entries_for_base(conn, event_id, base)
        alerts.append({
            "station_key": row["station_key"],
            "packets": row["packets"],
            "last_at": row["last_at"],
            "symbol": symbols.describe(row["symbol_table"], row["symbol_code"]),
            # A digipeater or igate under a rostered callsign is almost always
            # infrastructure to dismiss, not a person to adopt.
            "looks_like_infrastructure": symbols.is_infrastructure(
                row["symbol_table"], row["symbol_code"]
            ),
            "roster_candidates": [
                {"station_key": entry["station_key"],
                 "display_label": entry["display_label"],
                 "category": entry["category"]}
                for entry in candidates
            ],
        })
    return alerts


def build_state(conn: sqlite3.Connection, event_id: int) -> dict[str, Any]:
    """Everything a client needs to draw the map from scratch.

    Clients call this on connect AND on every reconnect, rather than trying to
    replay missed messages. A phone coming out of a dead zone gets a correct
    picture instead of a plausible-looking stale one.
    """
    event = conn.execute(
        "SELECT * FROM event WHERE id = ?", (event_id,)
    ).fetchone()

    courses = [
        {
            "id": row["id"],
            "name": row["name"],
            "color": row["color"],
            "dash_pattern": row["dash_pattern"],
            "distance_m": row["distance_m"],
            "sort_order": row["sort_order"],
            "geojson": json.loads(row["geojson"]),
        }
        for row in conn.execute(
            "SELECT * FROM course WHERE event_id = ? ORDER BY sort_order, id",
            (event_id,),
        ).fetchall()
    ]

    index = progress.CourseIndex.for_event(conn, event_id)

    # Aid stations are listed in COURSE order, not by name: see
    # CourseIndex.order_along_course for why name ordering cannot work.
    poi_rows = conn.execute(
        "SELECT * FROM poi WHERE event_id = ?", (event_id,)
    ).fetchall()
    pois = []
    for row in index.order_along_course(poi_rows):
        entry = dict(row)
        entry["course_position"] = course_position(index, row["lat"], row["lon"])
        # One or two characters for the pin itself. Derived unless the club
        # typed an override; the client never has to guess.
        entry["label_text"] = poi_labels.for_poi(row["name"], row["label"])
        pois.append(entry)

    roster = []
    for row in conn.execute(
        "SELECT * FROM roster WHERE event_id = ? ORDER BY category, display_label",
        (event_id,),
    ).fetchall():
        entry = dict(row)
        # Wording differs by category: an aid station is "Torn down", a sweep is
        # "Finished". The client should not have to know that mapping.
        entry["op_status_label"] = db.op_status_label(row["category"], row["op_status"])
        # What this station's packets arrive under. Equal to station_key unless
        # the roster named a bare callsign and an SSID has been heard for it.
        # The client joins positions on this; writes still name station_key.
        entry["tracking_key"] = db.tracking_key(row)
        # An operator posted at an aid station inherits that station's place on
        # the course, so the roster can be read in course order too.
        if row["poi_id"] is not None:
            poi = next((p for p in pois if p["id"] == row["poi_id"]), None)
            if poi is not None:
                entry["course_position"] = poi["course_position"]
        roster.append(entry)

    # Ignoring an SSID has to hide what was already stored, not merely stop
    # future packets. Otherwise "ignore the digipeater" leaves the digipeater
    # sitting on the map, which is not what the word promises.
    ignored = db.excluded_station_keys(conn, event_id)

    positions = [
        {
            "station_key": row["station_key"],
            "received_at": row["received_at"],
            "lat": row["lat"],
            "lon": row["lon"],
            "course_deg": row["course_deg"],
            "speed_kmh": row["speed_kmh"],
            "altitude_m": row["altitude_m"],
            "symbol_table": row["symbol_table"],
            "symbol_code": row["symbol_code"],
            "comment": row["comment"],
            "course_position": course_position(index, row["lat"], row["lon"]),
        }
        for row in db.latest_position_per_station(conn, event_id)
        if row["station_key"] not in ignored
    ]

    incident_rows = []
    for row in incidents.for_event(conn, event_id):
        entry = incidents.Incident(row).as_dict()
        # "bib 1432, mile 9.1 of Half" is dramatically more actionable over a
        # radio net than a lat/lon.
        entry["course_position"] = course_position(index, row["lat"], row["lon"])
        incident_rows.append(entry)

    return {
        "type": "state",
        "event": {
            "slug": event["slug"],
            "name": event["name"],
            "timezone": event["timezone"],
            "center_lat": event["center_lat"],
            "center_lon": event["center_lon"],
            "zoom": event["zoom"],
        },
        "courses": courses,
        # A club's own wording for the station roles. The keys are fixed
        # because each carries its own status vocabulary; the names are not.
        "role_labels": categories.role_labels(conn, event_id),
        # The layers this event has, and how each draws. Sent rather than
        # assumed, because the set is the club's, not the code's.
        "poi_categories": [
            {
                "key": row["key"],
                "name": row["name"],
                "staffed": bool(row["staffed"]),
                "icon": row["icon"],
                "color": row["color"],
                "visible": bool(row["visible"]),
                # Per layer: a dozen aid stations are worth labelling, fifty
                # mile markers would bury the map in digits.
                "show_labels": bool(row["show_labels"]),
            }
            for row in categories.poi_categories(conn, event_id)
        ],
        "pois": pois,
        "roster": roster,
        "positions": positions,
        # ssid_alerts is NOT here: it is roster-adjacent and only a role
        # holding CAP_SSID can act on it, so `state()` adds it for those
        # roles alone - left out for the rest, like everything role-gated.
        "leaders": [entry.as_dict() for entry in
                    leaders.for_event(conn, event_id, index)],
        # Which leaders the event tracks is not sent as its own list: each
        # `leaders` entry carries its `division` and `division_label`, which
        # is the only form the panel reads (a row per race per leader).
        "incidents": incident_rows,
        # For every role, Staff included: the one list the forwarded link
        # both reads and writes. Nothing here is acted on during the race.
        "event_notes": [event_notes.EventNote(row).as_dict()
                        for row in event_notes.for_event(conn, event_id)],
        "incident_statuses": [
            {"value": value, "label": incidents.STATUS_LABELS[value]}
            for value in incidents.STATUSES
        ],
        # No count of waiting pickups: the client derives it from the list
        # (`incidentDone` in app.js) and has to, because the list changes
        # under it on every socket message and a count sent once would be
        # stale by the second one.
        "op_statuses": list(db.OP_STATUSES),
        "thresholds": {
            "stale_after_s": STALE_AFTER_SECONDS,
            "silent_after_s": SILENT_AFTER_SECONDS,
        },
    }



def nearby_for(conn: sqlite3.Connection, event_id: int,
               store: dict) -> list[dict[str, Any]]:
    """The public heard near the course, minus anyone the roster has since
    named or dismissed. `store` is `app.state.nearby`, memory only."""
    known = set(db.all_station_keys(conn, event_id))
    known |= db.bound_station_keys(conn, event_id)
    known |= db.excluded_station_keys(conn, event_id)
    entries = store.get(event_id, {})
    for key in [k for k in entries if k in known]:
        entries.pop(key, None)         # assigned or dismissed since heard
    return sorted(
        entries.values(),
        key=lambda e: (e["course_position"] is None,
                       (e["course_position"] or {}).get("offset_m", 0)),
    )
