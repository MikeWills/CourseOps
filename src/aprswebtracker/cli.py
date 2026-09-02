"""Command-line entry point. Phase 1 is ingest only; no web server yet."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import aprsis, db, units
from .config import Settings, load_dotenv

CATEGORIES = [
    "net_control", "aid_station", "sweep", "sag", "shadow", "rover", "start_finish",
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
