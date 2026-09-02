"""Command-line entry point. No web server yet; that arrives in Phase 3."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import aprsis, db, importer, kml, units, what3words
from .config import Settings, load_dotenv

CATEGORIES = [
    "net_control", "aid_station", "sweep", "sag", "shadow", "rover", "start_finish",
]

POI_TYPES = [
    "aid_station", "start", "finish", "start_finish", "medical", "parking", "other",
]


def _settings() -> Settings:
    load_dotenv()
    return Settings.from_env()


def cmd_init_db(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    print(f"Schema ready at {settings.db_path}")
    return 0


def cmd_add_event(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    if db.get_event(conn, args.slug):
        print(f"Event {args.slug!r} already exists.", file=sys.stderr)
        return 1
    event_id = db.create_event(
        conn, args.slug, args.name,
        event_date=args.date, timezone=args.timezone,
        center_lat=args.lat, center_lon=args.lon,
        aprs_filter_extra=args.extra_filter,
    )
    print(f"Created event {args.slug!r} (id={event_id})")
    return 0


def cmd_add_station(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = db.get_event(conn, args.event)
    if event is None:
        print(f"No event with slug {args.event!r}", file=sys.stderr)
        return 1
    db.upsert_roster_entry(
        conn, event["id"], args.callsign, args.label,
        category=args.category,
        expects_aprs=not args.no_aprs,
        operator_name=args.operator,
    )
    tracking = "not tracked by APRS" if args.no_aprs else "tracked"
    print(f"{args.callsign.upper()} -> {args.label} [{args.category}, {tracking}]")
    return 0


def cmd_roster(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = db.get_event(conn, args.event)
    if event is None:
        print(f"No event with slug {args.event!r}", file=sys.stderr)
        return 1
    rows = db.roster_for_event(conn, event["id"])
    if not rows:
        print("Roster is empty.")
        return 0
    print(f"{'CALLSIGN':<12} {'LABEL':<22} {'CATEGORY':<14} APRS")
    for row in rows:
        print(
            f"{row['station_key']:<12} {row['display_label']:<22} "
            f"{row['category']:<14} {'yes' if row['expects_aprs'] else 'no'}"
        )
    keys = db.tracked_station_keys(conn, event["id"])
    print(f"\n{len(rows)} assigned, {len(keys)} expected to beacon.")
    print(f"Filter: {aprsis.build_filter(keys, event['aprs_filter_extra'])}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    from .ingest import run_ingest

    settings = _settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run_ingest(settings, args.event, max_packets=args.max_packets))
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = db.get_event(conn, args.event)
    if event is None:
        print(f"No event with slug {args.event!r}", file=sys.stderr)
        return 1
    rows = (
        db.latest_position_per_station(conn, event["id"])
        if args.latest
        else db.recent_positions(conn, event["id"], args.limit)
    )
    if not rows:
        print("No positions stored yet.")
        return 0
    print(f"{'RECEIVED':<21} {'STATION':<12} {'LAT':>10} {'LON':>11} {'SPEED':>8} {'ALT':>10}")
    for row in rows:
        print(
            f"{row['received_at']:<21} {row['station_key']:<12} "
            f"{row['lat']:>10.5f} {row['lon']:>11.5f} "
            f"{units.format_speed(row['speed_kmh']):>8} "
            f"{units.format_altitude(row['altitude_m']):>10}"
        )
    return 0


def _event_or_exit(conn, slug: str):
    event = db.get_event(conn, slug)
    if event is None:
        print(f"No event with slug {slug!r}", file=sys.stderr)
        raise SystemExit(1)
    return event


def cmd_import(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    event = _event_or_exit(conn, args.event)

    try:
        summary = importer.stage_file(conn, event["id"], args.file)
    except kml.KmlError as exc:
        print(f"Could not import {args.file}: {exc}", file=sys.stderr)
        return 1

    types = ", ".join(f"{n} {t}" for t, n in sorted(summary.by_type.items()))
    print(f"Staged {summary.total} features from {summary.filename} ({types}).")
    for warning in summary.warnings:
        print(f"  warning: {warning}")
    print(f"\nReview them with:  awt review {args.event}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    rows = importer.pending_features(conn, event["id"], include_all=args.all)
    if not rows:
        print("Nothing staged for review.")
        return 0

    print(f"{'ID':>4}  {'TYPE':<11} {'LENGTH':>9}  {'SUGGESTION':<18} NAME")
    for row in rows:
        length = units.format_distance(row["length_m"]) if row["length_m"] else ""
        flag = " *" if row["warnings"] else ""
        status = "" if row["status"] == "pending" else f" [{row['status']}]"
        print(
            f"{row['id']:>4}  {row['geom_type']:<11} {length:>9}  "
            f"{row['suggestion'] or 'unassigned':<18} {row['name']}{status}{flag}"
        )
        if row["folder"]:
            print(f"      in: {row['folder']}")
        if row["warnings"] and args.verbose:
            for line in row["warnings"].splitlines():
                print(f"      ! {line}")

    print("\nSuggestions are advisory - confirm each one. Assign with:")
    print(f"  awt assign-course {args.event} <id> [<id>...] --name NAME")
    print(f"  awt assign-poi    {args.event} <id> --type aid_station")
    print(f"  awt discard       {args.event} <id> [<id>...]")
    if any(r["warnings"] for r in rows) and not args.verbose:
        print("\n* has warnings; re-run with --verbose to see them.")
    return 0


def cmd_assign_course(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    try:
        course_id, distance_m, warnings = importer.assign_course(
            conn, event["id"], args.ids, args.name, args.color, args.reverse
        )
    except ValueError as exc:
        print(f"Could not build course: {exc}", file=sys.stderr)
        return 1

    print(f"Course {args.name!r} (id={course_id}): {units.format_distance(distance_m)}")
    for warning in warnings:
        print(f"  warning: {warning}")
    if warnings:
        print("  Check the stitched course on the map before the event.")
    return 0


def cmd_assign_poi(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)

    if args.what3words and not what3words.is_plausible(args.what3words):
        print(
            f"{args.what3words!r} does not look like a What3Words address "
            "(expected three dot-separated words).",
            file=sys.stderr,
        )
        return 1
    try:
        poi_id = importer.assign_poi(
            conn, event["id"], args.id, args.type, args.name,
            what3words.normalize(args.what3words),
        )
    except ValueError as exc:
        print(f"Could not create POI: {exc}", file=sys.stderr)
        return 1
    print(f"POI created (id={poi_id}, type={args.type}).")
    return 0


def cmd_discard(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    _event_or_exit(conn, args.event)
    count = importer.discard(conn, args.ids)
    print(f"Discarded {count} feature(s).")
    return 0


def cmd_courses(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    courses = conn.execute(
        "SELECT * FROM course WHERE event_id = ? ORDER BY sort_order, id",
        (event["id"],),
    ).fetchall()
    pois = conn.execute(
        "SELECT * FROM poi WHERE event_id = ? ORDER BY poi_type, name",
        (event["id"],),
    ).fetchall()

    if courses:
        print("COURSES")
        for row in courses:
            print(f"  {row['id']:>3}  {row['name']:<20} "
                  f"{units.format_distance(row['distance_m'])}")
    else:
        print("No courses yet.")

    if pois:
        print("\nPOINTS OF INTEREST")
        for row in pois:
            w3w = what3words.format_for_display(row["what3words"])
            print(f"  {row['id']:>3}  {row['name']:<24} {row['poi_type']:<13} "
                  f"{row['lat']:>9.5f},{row['lon']:>11.5f}  {w3w}")
    return 0


def cmd_set_w3w(args: argparse.Namespace) -> int:
    """What3Words is an NCS-maintained field, entered by hand."""
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    if not what3words.is_plausible(args.words):
        print(
            f"{args.words!r} does not look like a What3Words address "
            "(expected three dot-separated words).",
            file=sys.stderr,
        )
        return 1
    cur = conn.execute(
        "UPDATE poi SET what3words = ? WHERE id = ? AND event_id = ?",
        (what3words.normalize(args.words), args.poi_id, event["id"]),
    )
    if cur.rowcount == 0:
        print(f"No POI with id {args.poi_id} in this event.", file=sys.stderr)
        return 1
    print(f"POI {args.poi_id}: {what3words.format_for_display(args.words)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="awt", description="AprsWebTracker")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create the database and schema").set_defaults(
        func=cmd_init_db
    )

    p = sub.add_parser("add-event", help="create an event")
    p.add_argument("slug")
    p.add_argument("name")
    p.add_argument("--date", help="YYYY-MM-DD")
    p.add_argument("--timezone", default="UTC")
    p.add_argument("--lat", type=float, help="map center latitude")
    p.add_argument("--lon", type=float, help="map center longitude")
    p.add_argument(
        "--extra-filter",
        help="extra APRS-IS filter appended to the roster buddy filter, "
             "e.g. 'r/34.73/-86.58/30'",
    )
    p.set_defaults(func=cmd_add_event)

    p = sub.add_parser("add-station", help="add or update a roster entry")
    p.add_argument("event", help="event slug")
    p.add_argument("callsign", help="SSID-qualified, e.g. N0CALL-9")
    p.add_argument("label", help="display label, e.g. 'Half-back'")
    p.add_argument("--category", choices=CATEGORIES, default="rover")
    p.add_argument("--operator", help="operator name")
    p.add_argument(
        "--no-aprs",
        action="store_true",
        help="assigned but not beaconing; excluded from the filter and from "
             "staleness alerting (typical for aid station operators)",
    )
    p.set_defaults(func=cmd_add_station)

    p = sub.add_parser("roster", help="show the roster and resulting filter")
    p.add_argument("event")
    p.set_defaults(func=cmd_roster)

    p = sub.add_parser("ingest", help="connect to APRS-IS and store positions")
    p.add_argument("event")
    p.add_argument(
        "--max-packets", type=int,
        help="stop after this many lines (for smoke tests)",
    )
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("import", help="stage a KML/KMZ file for review")
    p.add_argument("event")
    p.add_argument("file", help="path to a .kml or .kmz file")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("review", help="list staged features awaiting assignment")
    p.add_argument("event")
    p.add_argument("--all", action="store_true", help="include assigned/discarded")
    p.add_argument("--verbose", action="store_true", help="show parser warnings")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser(
        "assign-course", help="build a course from one or more staged line features"
    )
    p.add_argument("event")
    p.add_argument("ids", type=int, nargs="+", help="staged feature ids to stitch")
    p.add_argument("--name", required=True, help="e.g. 'Half'")
    p.add_argument("--color", help="hex color for the map")
    p.add_argument(
        "--reverse", action="store_true", help="flip a line drawn finish-to-start"
    )
    p.set_defaults(func=cmd_assign_course)

    p = sub.add_parser("assign-poi", help="turn a staged point into a POI")
    p.add_argument("event")
    p.add_argument("id", type=int)
    p.add_argument("--type", choices=POI_TYPES, default="aid_station")
    p.add_argument("--name", help="override the imported name")
    p.add_argument("--what3words", help="e.g. filled.count.soap")
    p.set_defaults(func=cmd_assign_poi)

    p = sub.add_parser("discard", help="mark staged features as not needed")
    p.add_argument("event")
    p.add_argument("ids", type=int, nargs="+")
    p.set_defaults(func=cmd_discard)

    p = sub.add_parser("courses", help="show imported courses and POIs")
    p.add_argument("event")
    p.set_defaults(func=cmd_courses)

    p = sub.add_parser("set-w3w", help="set a POI's What3Words address (NCS)")
    p.add_argument("event")
    p.add_argument("poi_id", type=int)
    p.add_argument("words", help="e.g. filled.count.soap")
    p.set_defaults(func=cmd_set_w3w)

    p = sub.add_parser("tail", help="show stored positions")
    p.add_argument("event")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--latest", action="store_true", help="newest per station")
    p.set_defaults(func=cmd_tail)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
