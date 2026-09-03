"""Command-line entry point. No web server yet; that arrives in Phase 3."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import (access, aprsis, categories, db, discovery, importer, kml,
               leaders, styling, units, users, what3words)
from .config import Settings, load_dotenv

# Station roles are a fixed set - each carries its own status wording - so the
# CLI can still offer them as choices. Their *names* are per event and edited in
# the setup UI; the CLI works in keys.
CATEGORIES = [key for key, _ in categories.DEFAULT_ROSTER_ROLES]

# Place layers are deliberately NOT a fixed list: a club adds its own, so there
# is nothing here to enumerate. `--type` takes any layer key that exists in the
# event, and `courseops layers <event>` prints them.


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


async def _run_check_in(settings, event, seconds: int):
    """Listen wide for a few minutes and record every SSID heard."""
    from .parser import Rejected, parse_packet

    conn = db.connect(settings.db_path)
    roster_keys = db.all_station_keys(conn, event["id"])
    conn.close()
    if not roster_keys:
        raise SystemExit("Roster is empty - add stations before checking in.")

    settings.require_callsign()
    check = discovery.CheckIn(roster_keys)
    aprs_filter = discovery.wildcard_filter(roster_keys)
    print(f"Listening {seconds}s for every SSID of {len(check.expected_bases)} "
          f"callsign(s)...")
    print(f"Filter: {aprs_filter}\n")

    async def listen():
        async for line in aprsis.stream_packets(
            settings.host, settings.port, settings.callsign,
            settings.passcode, aprs_filter,
        ):
            try:
                check.observe(parse_packet(line))
            except Rejected:
                pass          # status, messages, telemetry: not useful here

    try:
        await asyncio.wait_for(listen(), timeout=seconds)
    except asyncio.TimeoutError:
        pass                  # the timeout IS the end of the listen
    return check


def cmd_check_in(args: argparse.Namespace) -> int:
    """Find out which SSIDs the roster's people are actually transmitting on.

    Run this a week out, at a club meeting. It turns a silent race-morning
    failure into something you can fix while there is still time.
    """
    settings = _settings()
    logging.basicConfig(level=logging.WARNING)
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    conn.close()

    try:
        check = asyncio.run(_run_check_in(settings, event, args.seconds))
    except KeyboardInterrupt:
        print("\nStopped early.")
        return 1

    def line_for(entry):
        where = (f"{entry.last_lat:.4f},{entry.last_lon:.4f}"
                 if entry.last_lat is not None else "no position")
        return (f"    {entry.station_key:<12} {entry.positions:>3} pos  "
                f"{entry.description:<22} {where}")

    confirmed = check.confirmed()
    mismatched = check.mismatched()
    others = check.other_ssids()
    silent = [k for k in check.silent() if k not in mismatched]

    print("=" * 68)
    if confirmed:
        print(f"\nHEARD, ON THE ROSTER  ({len(confirmed)})")
        for entry in sorted(confirmed, key=lambda e: e.station_key):
            print(line_for(entry))

    if mismatched:
        print(f"\nWRONG SSID ON THE ROSTER  ({len(mismatched)})")
        print("  These are on the roster but silent, while another SSID of the")
        print("  same callsign is transmitting. Almost certainly a typo at signup.")
        for rostered, candidates in mismatched.items():
            print(f"\n    roster says {rostered}, but heard:")
            for entry in candidates:
                print(line_for(entry))
            best = candidates[0]
            print(f"      fix:  courseops add-station {args.event} "
                  f"{best.station_key} \"<label>\" --category <category>")
            print(f"      then: courseops remove-station {args.event} {rostered}")

    if others:
        print(f"\nOTHER SSIDS UNDER THESE CALLSIGNS  ({len(others)})")
        print("  Not on the roster. Usually the operator's own digipeater, igate")
        print("  or home station - listed so it is a decision, not a surprise.")
        for entry in sorted(others, key=lambda e: e.station_key):
            flag = "  <- infrastructure, ignore" if entry.looks_like_infrastructure else ""
            print(line_for(entry) + flag)

    if silent:
        print(f"\nNOT HEARD AT ALL  ({len(silent)})")
        print("  Either not transmitting right now, or not beaconing at all.")
        print("  Aid station operators marked --no-aprs are expected here.")
        for key in silent:
            print(f"    {key}")

    print("\n" + "=" * 68)
    if mismatched:
        print("Action needed: fix the SSIDs above before race day.")
    elif not confirmed:
        print("Nothing heard. Check the callsigns, or listen longer with --seconds.")
    else:
        print("Roster looks correct.")
    return 0


def cmd_ignore(args: argparse.Namespace) -> int:
    """Keep an SSID off the map: a digipeater, igate or home station."""
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    if args.remove:
        if db.unexclude_station(conn, event["id"], args.callsign):
            print(f"{args.callsign.upper()} will be tracked again.")
            return 0
        print(f"{args.callsign.upper()} was not ignored.", file=sys.stderr)
        return 1
    db.exclude_station(conn, event["id"], args.callsign, args.reason)
    print(f"Ignoring {args.callsign.upper()}"
          + (f" ({args.reason})" if args.reason else "") + ".")
    return 0


def cmd_ignored(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    rows = db.exclusions(conn, event["id"])
    if not rows:
        print("Nothing ignored. The filter asks for every SSID of each rostered")
        print("callsign, so a digipeater or igate will appear until it is ignored.")
        return 0
    print(f"{'CALLSIGN':<12} REASON")
    for row in rows:
        print(f"{row['station_key']:<12} {row['reason'] or ''}")
    return 0


def cmd_remove_station(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    cur = conn.execute(
        "DELETE FROM roster WHERE event_id = ? AND station_key = ?",
        (event["id"], args.callsign.upper()),
    )
    if cur.rowcount == 0:
        print(f"{args.callsign.upper()} is not on this roster.", file=sys.stderr)
        return 1
    print(f"Removed {args.callsign.upper()} from the roster.")
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
    print(f"\nReview them with:  courseops review {args.event}")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    rows = importer.pending_features(conn, event["id"], include_all=args.all)
    if not rows:
        print("Nothing staged for review.")
        return 0

    duplicated_names: dict[str, int] = {}
    for row in rows:
        duplicated_names[row["name"]] = duplicated_names.get(row["name"], 0) + 1

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
        # Exporters give placemarks identical names and distinguish them only by
        # style, so show it whenever a name is not unique in this listing.
        if row["style_id"] and duplicated_names.get(row["name"], 0) > 1:
            print(f"      style: {row['style_id']}")
        if row["warnings"] and args.verbose:
            for line in row["warnings"].splitlines():
                print(f"      ! {line}")

    print("\nSuggestions are advisory - confirm each one. Assign with:")
    print(f"  courseops assign-course {args.event} <id> [<id>...] --name NAME")
    print(f"  courseops assign-poi    {args.event} <id> --type aid_station")
    print(f"  courseops discard       {args.event} <id> [<id>...]")
    if any(r["warnings"] for r in rows) and not args.verbose:
        print("\n* has warnings; re-run with --verbose to see them.")
    return 0


def cmd_assign_course(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    try:
        course_id, distance_m, warnings = importer.assign_course(
            conn, event["id"], args.ids, args.name, args.color, args.reverse,
            args.dash,
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


def cmd_layers(args: argparse.Namespace) -> int:
    """The place layers and role names this event has.

    Place layers are the club's own list, so there is nothing to hardcode in
    `--type`'s help text - this is how you find out what exists.
    """
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    rows = categories.poi_categories(conn, event["id"])
    conn.commit()

    print()
    print(f"Place layers for {event['name']!r}")
    print()
    print(f"  {'KEY':<20} {'NAME':<22} {'STAFFED':<9} PLACES")
    for row in rows:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM poi WHERE event_id = ? AND poi_type = ?",
            (event["id"], row["key"]),
        ).fetchone()["c"]
        staffed = "yes" if row["staffed"] else "-"
        print(f"  {row['key']:<20} {row['name']:<22} {staffed:<9} {count}")

    print()
    print("Station roles")
    print()
    for row in categories.roster_roles(conn, event["id"]):
        print(f"  {row['key']:<20} {row['name']}")
    print()
    print("Add layers and rename either of these in the setup UI: /setup")
    print()
    conn.close()
    return 0



def cmd_courses(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    courses = conn.execute(
        "SELECT * FROM course WHERE event_id = ? ORDER BY sort_order, id",
        (event["id"],),
    ).fetchall()
    from . import progress
    index = progress.CourseIndex.for_event(conn, event["id"])
    pois = index.order_along_course(
        conn.execute("SELECT * FROM poi WHERE event_id = ?", (event["id"],)).fetchall()
    )

    if courses:
        print("COURSES  (listed in draw order: last is on top)")
        print(f"  {'ID':>3}  {'ORD':>3}  {'NAME':<20} {'DISTANCE':>9}  "
              f"{'COLOR':<9} STYLE")
        for row in courses:
            print(
                f"  {row['id']:>3}  {row['sort_order']:>3}  {row['name']:<20} "
                f"{units.format_distance(row['distance_m']):>9}  "
                f"{row['color'] or '(none)':<9} "
                f"{styling.describe_dash(row['dash_pattern'])}"
            )
        print("\n  Restyle:  courseops style-course <event> <id> [--color #cc3333] "
              "[--dash dotted]")
        print("  Reorder:  courseops style-course <event> <id> --order N   "
              "(higher draws on top)")
    else:
        print("No courses yet.")

    if pois:
        print("\nPOINTS OF INTEREST  (in course order, not by name)")
        print(f"  {'ID':>3}  {'NAME':<24} {'TYPE':<13} {'MILE':>8}  WHAT3WORDS")
        for row in pois:
            located = index.locate(row["lat"], row["lon"])
            mile = f"{located.distance_along_m / 1609.344:.1f}" if located else "--"
            w3w = what3words.format_for_display(row["what3words"])
            print(f"  {row['id']:>3}  {row['name']:<24} {row['poi_type']:<13} "
                  f"{mile:>8}  {w3w}")
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    """Post a roster entry at an aid station."""
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    try:
        row = db.assign_station_to_poi(
            conn, event["id"], args.callsign, None if args.clear else args.poi_id
        )
    except ValueError as exc:
        print(f"Could not post station: {exc}", file=sys.stderr)
        return 1
    if row["poi_id"] is None:
        print(f"{row['station_key']} is no longer posted at an aid station.")
        return 0
    poi = conn.execute("SELECT name FROM poi WHERE id = ?", (row["poi_id"],)).fetchone()
    print(f"{row['station_key']} ({row['display_label']}) posted at {poi['name']}.")
    return 0


def cmd_style_course(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    try:
        row = importer.set_course_style(
            conn, event["id"], args.id,
            color=args.color, dash=args.dash, name=args.name,
            sort_order=args.order,
        )
    except ValueError as exc:
        print(f"Could not update course: {exc}", file=sys.stderr)
        return 1
    print(
        f"Course {row['id']} {row['name']!r}: color {row['color'] or '(none)'}, "
        f"{styling.describe_dash(row['dash_pattern'])}, draw order {row['sort_order']}"
    )
    return 0


def cmd_bib_color(args: argparse.Namespace) -> int:
    """Pre-set the bib colour for a race, so race day is one tap."""
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    try:
        row = leaders.set_bib_color(
            conn, event["id"], args.course_id, args.color, args.name
        )
    except ValueError as exc:
        print(f"Could not set bib colour: {exc}", file=sys.stderr)
        return 1
    label = row["bib_color_name"] or row["bib_color"]
    print(f"{row['name']}: bibs are {label} ({row['bib_color']})")
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


def cmd_links(args: argparse.Namespace) -> int:
    """Show the role URLs to paste into the right group texts."""
    settings = _settings()
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    event = _event_or_exit(conn, args.event)

    if args.new:
        token = access.create_token(conn, event["id"], args.new)
        print(f"New {access.ROLE_LABELS[args.new]} link created.\n")

    tokens = access.ensure_tokens(conn, event["id"])
    base = args.base_url.rstrip("/")
    print(f"Access links for {event['name']!r}:\n")
    for role in access.ROLES:
        print(f"  {access.ROLE_LABELS[role]}")
        print(f"    {base}/e/{event['slug']}/{tokens[role]}\n")
    print("These are bearer links - anyone holding one has that role.")
    print("Send each to the right group only. Revoke with:  courseops revoke-link <event> <id>")

    revoked = [r for r in access.tokens_for_event(conn, event["id"]) if r["revoked"]]
    if revoked:
        print(f"\n({len(revoked)} revoked link(s) not shown.)")
    return 0


def cmd_list_links(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    event = _event_or_exit(conn, args.event)
    rows = access.tokens_for_event(conn, event["id"])
    if not rows:
        print("No links yet. Create them with:  courseops links <event>")
        return 0
    print(f"{'ID':>3}  {'ROLE':<9} {'STATUS':<8} {'LAST USED':<21} TOKEN")
    for row in rows:
        status = "revoked" if row["revoked"] else "active"
        print(
            f"{row['id']:>3}  {row['role']:<9} {status:<8} "
            f"{row['last_used'] or 'never':<21} {row['token'][:12]}..."
        )
    return 0


def cmd_revoke_link(args: argparse.Namespace) -> int:
    settings = _settings()
    conn = db.connect(settings.db_path)
    _event_or_exit(conn, args.event)
    if access.revoke(conn, args.token_id):
        print(f"Link {args.token_id} revoked. Anyone holding it now gets a 404.")
        return 0
    print(f"No link with id {args.token_id}.", file=sys.stderr)
    return 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web import create_app

    settings = _settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    conn = db.connect(settings.db_path)
    db.init_schema(conn)

    # --base-url exists so shared links can carry a public domain. When it is
    # left at the default but the port is not, the printed addresses would
    # point at a port nothing is listening on - so derive it instead.
    base = args.base_url.rstrip("/")
    if base == "http://localhost:8000" and args.port != 8000:
        host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
        base = f"http://{host}:{args.port}"

    ingest_events = []
    lines = []

    if args.event:
        event = _event_or_exit(conn, args.event)
        tokens = access.ensure_tokens(conn, event["id"])
        lines.append(f"Event: {event['name']}")
        lines.append("")
        for role in access.ROLES:
            lines.append(
                f"  {access.ROLE_LABELS[role]:<14} "
                f"{base}/e/{event['slug']}/{tokens[role]}"
            )
        if args.no_ingest:
            lines.append("")
            lines.append("  APRS-IS ingest disabled (--no-ingest).")
        elif settings.callsign_problem:
            # Everything except live tracking works without a callsign, so the
            # server still starts. Saying so plainly beats refusing to run,
            # which is what a first-time user meets otherwise.
            lines.append("")
            lines.append("  NOT tracking - " + settings.callsign_problem)
        else:
            ingest_events = [args.event]
            lines.append("")
            lines.append(f"  Ingesting APRS-IS as {settings.callsign} (receive only).")
    else:
        # No event named: the server still runs, and this is the case that used
        # to print nothing at all - a blank terminal with no sign it had
        # started and no address to open.
        lines.append("No event named, so no APRS-IS connection was opened.")
        lines.append("Add one with:  courseops serve <event>")

    needs_first_user = not users.any_users(conn)
    conn.close()

    print()
    print("  Course Ops")
    print(f"  Setup: {base}/setup")
    if needs_first_user:
        print("         (first run - it will ask you to create an administrator)")
    print()
    for line in lines:
        print(line if not line else f"  {line}" if not line.startswith("  ") else line)
    print()
    print(f"  Listening on http://{args.host}:{args.port}   Ctrl-C to stop")
    if args.host in ("127.0.0.1", "localhost"):
        print("  Only this machine can reach it. For a phone on the same wifi,")
        print("  add --host 0.0.0.0 (the location dot still needs HTTPS).")
    print()

    app = create_app(settings, ingest_events=ingest_events)

    # Behind a reverse proxy the app is spoken to in plain HTTP on localhost.
    # Without this, X-Forwarded-Proto is ignored, request.url.scheme stays
    # "http", and session cookies never get the Secure flag in exactly the
    # deployment where it matters.
    #
    # `forwarded_allow_ips` is what stops any client simply claiming HTTPS:
    # only the named proxy is believed. It defaults to the loopback address,
    # which is the normal Apache-on-the-same-host case.
    if args.behind_proxy:
        print(f"Trusting proxy headers from {args.trusted_proxy}.")
        if args.host not in ("127.0.0.1", "::1", "localhost"):
            print(
                f"  warning: listening on {args.host}, which is reachable "
                "directly.\n"
                "  Behind a proxy this should bind 127.0.0.1 so the only way "
                "in is through it.",
                file=sys.stderr,
            )
        uvicorn.run(
            app, host=args.host, port=args.port, log_level="warning",
            proxy_headers=True, forwarded_allow_ips=args.trusted_proxy,
        )
    else:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="courseops", description="Course Ops")
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

    p = sub.add_parser(
        "check-in",
        help="listen for every SSID of your rostered callsigns and find wrong ones",
    )
    p.add_argument("event")
    p.add_argument(
        "--seconds", type=int, default=300,
        help="how long to listen; phone apps beacon every 1-5 minutes, so a "
             "short listen will miss people (default 300)",
    )
    p.set_defaults(func=cmd_check_in)

    p = sub.add_parser(
        "ignore", help="keep an SSID off the map (digipeater, igate, home station)"
    )
    p.add_argument("event")
    p.add_argument("callsign")
    p.add_argument("--reason", help="why, e.g. 'digipeater'")
    p.add_argument("--remove", action="store_true", help="stop ignoring it")
    p.set_defaults(func=cmd_ignore)

    p = sub.add_parser("ignored", help="list ignored SSIDs")
    p.add_argument("event")
    p.set_defaults(func=cmd_ignored)

    p = sub.add_parser("remove-station", help="remove a roster entry")
    p.add_argument("event")
    p.add_argument("callsign")
    p.set_defaults(func=cmd_remove_station)

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
    p.add_argument("--color", help="hex color; defaults to the next palette color")
    p.add_argument("--dash", help="line style (default solid); see style-course")
    p.add_argument(
        "--reverse", action="store_true", help="flip a line drawn finish-to-start"
    )
    p.set_defaults(func=cmd_assign_course)

    p = sub.add_parser("assign-poi", help="turn a staged point into a POI")
    p.add_argument("event")
    p.add_argument("id", type=int)
    p.add_argument("--type", default="aid_station",
                   help="Layer key; see: courseops layers <event>")
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

    p = sub.add_parser("layers", help="show this event's place layers and roles")
    p.add_argument("event")
    p.set_defaults(func=cmd_layers)

    p = sub.add_parser(
        "style-course", help="change a course's color, line style, name or draw order"
    )
    p.add_argument("event")
    p.add_argument("id", type=int, help="course id from 'courseops courses'")
    p.add_argument("--color", help="hex color, e.g. #cc3333")
    p.add_argument(
        "--dash",
        help="line style: " + ", ".join(styling.DASH_PRESETS)
             + ", or an SVG dasharray like '12,8'",
    )
    p.add_argument("--name", help="rename the course")
    p.add_argument(
        "--order", type=int,
        help="draw order; higher draws on top where courses share road",
    )
    p.set_defaults(func=cmd_style_course)

    p = sub.add_parser(
        "post", help="post a roster entry at an aid station (gives it a position)"
    )
    p.add_argument("event")
    p.add_argument("callsign")
    p.add_argument("poi_id", type=int, nargs="?", help="POI id from 'courseops courses'")
    p.add_argument("--clear", action="store_true", help="un-post the station")
    p.set_defaults(func=cmd_post)

    p = sub.add_parser(
        "bib-color", help="set a race's bib colour (defaults to the course colour)"
    )
    p.add_argument("event")
    p.add_argument("course_id", type=int)
    p.add_argument("--color", help="hex colour; omit to copy the course line colour")
    p.add_argument("--name", help="what people call it, e.g. Yellow")
    p.set_defaults(func=cmd_bib_color)

    p = sub.add_parser("set-w3w", help="set a POI's What3Words address (NCS)")
    p.add_argument("event")
    p.add_argument("poi_id", type=int)
    p.add_argument("words", help="e.g. filled.count.soap")
    p.set_defaults(func=cmd_set_w3w)

    p = sub.add_parser("links", help="show the role URLs for an event")
    p.add_argument("event")
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument(
        "--new", choices=access.ROLES,
        help="issue an additional link for this role before listing",
    )
    p.set_defaults(func=cmd_links)

    p = sub.add_parser("list-links", help="list access links and their status")
    p.add_argument("event")
    p.set_defaults(func=cmd_list_links)

    p = sub.add_parser("revoke-link", help="revoke an access link")
    p.add_argument("event")
    p.add_argument("token_id", type=int)
    p.set_defaults(func=cmd_revoke_link)

    p = sub.add_parser("serve", help="run the web server")
    p.add_argument(
        "event", nargs="?",
        help="event to ingest and print links for; omit to serve without ingest",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument(
        "--no-ingest", action="store_true",
        help="serve the map without opening an APRS-IS connection",
    )
    p.add_argument(
        "--behind-proxy", action="store_true",
        help="running behind Apache/nginx: honour X-Forwarded-Proto so session "
             "cookies get the Secure flag on HTTPS",
    )
    p.add_argument(
        "--trusted-proxy", default="127.0.0.1",
        help="which address may set forwarded headers (default: 127.0.0.1)",
    )
    p.set_defaults(func=cmd_serve)

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
