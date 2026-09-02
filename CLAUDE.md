# AprsWebTracker

Web map for marathon-style events: race courses and aid stations from organizer
KML/KMZ, overlaid with live APRS positions of the ham radio operators supporting
the event. Built to be stood up by a radio club without much effort.

Full plan, phase detail, and **known gaps / open threads**: `docs/PLAN.md`.
Complete history with the reasoning behind each fix: `CHANGELOG.md`.
Open work is also tracked as GitHub issues (issue #1: GPX import).

**Starting a fresh session?** Read `docs/PLAN.md` first - it carries the
decisions and the constraints discovered so far. The "Domain rules" section
below is the short list of things that cost real time when violated.

## Status

Phases 1-3 complete: APRS-IS ingest, KML/KMZ import, and the live map with
role-gated access. Repo: private, `MikeWills/AprsWebTracker`.

Phases: 1 ingest ✅ · 2 KML import ✅ · 3 live map ✅ · 4 roster/NCS panel ·
4a What3Words · 5 course-relative position · 6 incidents · 7 replay · 8 deployment

## Commands

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
cp .env.example .env                                    # then set APRS_CALLSIGN

./.venv/Scripts/python.exe -m pytest -q                 # 119 tests, no network

awt init-db
awt add-event marathon2026 "Spring Marathon 2026" --lat 34.73 --lon -86.58
awt add-station marathon2026 N0CALL-7 "Half-back" --category sweep
awt add-station marathon2026 KI4HMD-1 "Aid 4" --category aid_station --no-aprs
awt roster marathon2026          # shows the generated APRS-IS filter
awt ingest marathon2026          # live; --max-packets N for a smoke test
awt tail marathon2026 --latest

awt import marathon2026 course.kmz   # stage for review; additive across files
awt review marathon2026 --verbose    # suggestions are advisory only
awt assign-course marathon2026 1 3 --name "Half"   # stitches segments
awt assign-poi marathon2026 6 --type aid_station --what3words filled.count.soap
awt discard marathon2026 2 8
awt courses marathon2026
awt style-course marathon2026 1 --color "#cc3333" --order 10
awt set-w3w marathon2026 4 index.home.raft

awt links marathon2026           # the two role URLs to send out
awt serve marathon2026           # web server + live APRS-IS ingest
awt serve marathon2026 --no-ingest   # map only, no APRS-IS connection
awt list-links marathon2026 / awt revoke-link marathon2026 <id>
```

Tests never touch the network. Run `ingest` only when you actually want a live
APRS-IS connection.

## Layout

```
src/aprswebtracker/
  schema.sql      full domain schema, all tables event-scoped
  config.py       env settings, tiny .env loader (no dependency)
  parser.py       raw APRS text -> PositionReport, via aprslib
  aprsis.py       async APRS-IS client: filter, login, backoff
  db.py           SQLite access
  ingest.py       feed -> parse -> store; on_position hook for Phase 3 WebSocket
  geo.py          haversine, line length, segment stitching; (lon, lat) order
  kml.py          KML/KMZ parsing, hardened; classification is advisory only
  importer.py     two-phase import: stage for review, then commit assignments
  units.py        metric storage -> US customary display
  styling.py      course colors, line styles, draw order
  what3words.py   normalize/validate W3W strings, no API
  access.py       role tokens: ncs writes, liaison reads
  hub.py          per-event fan-out, bounded queues
  web.py          FastAPI: map page, state snapshot, WebSocket
  static/         Leaflet client, no build step
  cli.py          awt entry point
tests/fixtures/packets.txt          packet corpus, `expectation|raw` per line
tests/fixtures/messy_course.kml     synthetic KML with real organizer defects
tests/fixtures/mankato_marathon.kml REAL MapMyRun export, 26.4 mi; the only
                                    realistic course we have - use it for Phase 5
docs/PLAN.md                        plan, decisions, known gaps
```

## Domain rules that are easy to get wrong

These cost real debugging time if violated. Most are load-bearing for event-day
usability, not style preferences.

- **Receive-only, forever.** Passcode `-1` grants read access and no transmit
  capability. The club needs a callsign and no secret. Never add a real passcode.
- **One APRS-IS connection for the whole server.** Browsers connect to our
  WebSocket; the server fans out. APRS-IS bans clients that open many connections
  or reconnect tightly. Keep the backoff and the jitter.
- **The roster is not the APRS feed.** Three separate things: the aid station (a
  location from KML), the operator assigned to it (a callsign), and a position
  report (only if they beacon). **Most aid station operators never beacon.**
- **`expects_aprs=0` means "do not alert when silent", not "discard".** It gates
  staleness alerting and filter construction only. Use `tracked_station_keys` to
  build the filter and `all_station_keys` to decide whether to store. Getting this
  backwards was already a bug once.
- **Two independent status axes.** Operational (`pending`/`active`/`closed`, set
  by NCS) and radio (`fresh`/`stale`/`silent`/`n/a`, derived). Never merge them
  into one badge — "On station, no APRS" is healthy; "Rolling, silent 18 min" is
  an alarm.
- **SSID is part of station identity.** `N0CALL-9` and `N0CALL-7` are different
  radios on different people. Never key on the base callsign.
- **Symbol table and code travel as a pair.** The table character changes the
  meaning of the code.
- **Store metric, display US customary.** aprslib gives km/h and meters; that is
  what goes in SQLite. Convert only in `units.py` at the display edge.
- **Never interpolate marker movement.** Updates arrive every 1-5 minutes with
  gaps. Showing a position that was never reported is worse than showing a stale
  one, because someone will act on it. Sparse jumps are correct.
- **Buddy filter, not area filter.** We know the roster in advance. This avoids
  incidentally storing the public's location.
- **Keep medical detail out of incidents.** Bib, location, status, short
  operational note. Narrative descriptions of a runner's condition would make this
  a system storing health information about identifiable people.
- **Everything is event-scoped**, even with one event. `event_id` on every table.
- **No What3Words API.** Paid service, deliberately not integrated. Manual entry,
  shape validation only, KML lat/lon stays authoritative.
- **Import never writes directly to `course` or `poi`.** Files stage as `pending`
  in `import_feature`; a human assigns each one. `suggest()` is advisory and must
  stay conservative — better unassigned than a parking lot filed as an aid station.
- **KML is untrusted third-party input.** It comes from the race organizer and
  will arrive by web upload. Parse with `defusedxml`, keep the KMZ decompression
  and size guards, and never swap back to stdlib `ElementTree.fromstring`.
- **Coordinates are (lon, lat)** in `geo.py`, `kml.py` and GeoJSON — the reverse
  of how everyone speaks. The database stores `lat`/`lon` as named columns.
- **`geo.stitch` must grow the chain at both ends.** Growing only from the tail
  silently folds a course back on itself when the file lists a middle segment
  first. This was a real bug; there is a regression test.
- **Course overlap is solved by draw order, not by dashes.** The Full, Half and
  10K share road; `course.sort_order` decides which line wins (higher draws on
  top) and is adjustable. Courses are solid by default. Dash patterns exist as
  an opt-in for seeing two coincident routes at once.
- **Adding a schema column requires a migration entry.** `CREATE TABLE IF NOT
  EXISTS` skips existing tables, so a new column never reaches an existing
  database. Add it to `_ADDED_COLUMNS` in `db.py` as well as `schema.sql`.
- **`<styleUrl>` can be the only thing distinguishing two placemarks.** MapMyRun
  names the route, the start marker and the finish marker identically. Keep
  `style_id` captured, fed into `suggest()`, and shown in `review` for duplicate
  names.
- **Hint patterns must normalize `_` and `-` to spaces first.** `_` is a word
  character, so `start` never matches inside `start_marker`.
- **Liaison and Logistics are different teams, not one role.** Liaison is
  embedded with Public Safety and Medics; Logistics is in the field doing traffic
  control, cone placement and teardown. Separate links so one can be revoked
  alone. Both read-only in v1 (`WRITE_ROLES`).
- **Sweeps stay visible to Logistics.** The sweep is the back of the pack, so its
  position is what says a road is clear and the cones can come up.
- **An invalid token returns 404, never 403.** A 403 would confirm the event
  exists. Tokens are also scoped to their event: valid elsewhere means nothing.
- **Never interpolate marker movement in the client** (same rule as the plan).
  `setLatLng`, not an animated transition.
- **The connection badge must stay visible.** It is the only signal that a phone
  is showing stale data. Leaflet's zoom control shares that corner at z-index
  1000; the top bar reserves space for it.
- **`Subscription` needs `eq=False`.** Subscriptions live in a set, and two
  browsers on one event are distinct subscribers with identical fields.
- **Client escaping goes through `escapeHtml`,** which escapes quotes too - the
  textContent/innerHTML trick does not, and values land in attributes.
- **Geolocation needs a secure context.** Browsers block it over plain http://
  except on localhost. This constrains deployment (Phase 8): a club serving over
  a LAN without TLS loses the "where am I" dot. The client names the real cause.
- **The viewer's own position is local only** - never sent to the server, never
  stored, never visible to other viewers.
- **CLI output stays ASCII.** Em dashes become mojibake in the Windows console,
  and a club laptop is the target environment.

## Conventions

- Python 3.11+, `from __future__ import annotations`, dataclasses for value types.
- Dependencies stay minimal — every added package is one more thing a club has to
  install. Four runtime dependencies today (`aprslib`, `defusedxml`, `fastapi`,
  `uvicorn`), each with a stated reason; justify any addition. The frontend has
  no build step on purpose - no npm in a club's deployment.
- Comments explain *why*, especially where a choice looks arbitrary but is
  protecting against a real event-day failure.
- New parser edge cases get a line in `tests/fixtures/packets.txt`, not a bespoke
  test. Captured live traffic goes in `live_*.txt`, which is gitignored — it holds
  the positions of people who did not consent to a public repo.

## Recent changes

Last 10 entries; full record in `CHANGELOG.md`.

- **2026-09-02** Preserved the real Mankato course as a fixture with 8 regression tests.
- **2026-09-02** Added "Known gaps and open threads" to `docs/PLAN.md`.
- **2026-09-02** Split Logistics out as its own read-only role, separate from Liaison.
- **2026-09-02** Locate button now tracks continuously with `watchPosition`, plus an accuracy circle.
- **2026-09-02** Added a location status line so location errors no longer overwrite the connection badge.
- **2026-09-02** Phase 3: FastAPI server, role-gated access, WebSocket fan-out, Leaflet map client.
- **2026-09-02** Fixed: connection badge was hidden behind Leaflet's zoom control.
- **2026-09-02** Fixed: `Subscription` was unhashable; `@dataclass` unset `__hash__`.
- **2026-09-02** Added `access.py`, `hub.py`, `web.py` and the `serve`/`links` CLI commands.
- **2026-09-02** Added `styleUrl` capture; start/finish markers sharing a name are now distinguishable.
