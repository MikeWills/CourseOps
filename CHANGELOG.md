# Changelog

All notable changes to AprsWebTracker. Newest first. The ten most recent entries
are mirrored into `CLAUDE.md`; this file is the complete record.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### 2026-09-02 - Location tracking for the viewer

#### Changed
- The locate button now TRACKS the viewer with `watchPosition` instead of
  taking a single `getCurrentPosition` fix. Ops and Shadow are defined by
  moving around the course; a dot frozen where they tapped five minutes ago is
  worse than no dot, because it still looks current. A second tap turns it off
  and removes the dot.
- Added an accuracy circle around the dot, and a warning when the fix is worse
  than 100 m - a 500 m "fix" is wifi triangulation, not GPS, and should not be
  trusted as a position.

#### Added
- A dedicated location status line. Location problems previously overwrote the
  connection badge, which would have masked whether the data feed was live.
- Distinct messages for permission denied, no fix yet, and no location support,
  instead of one catch-all.
- An explicit check for a non-secure context. Browsers block geolocation outside
  HTTPS (localhost excepted), so a club serving this over plain http:// on a LAN
  would otherwise see a bare permission error that looks like the user's fault.

#### Fixed
- Removed a leftover branch that panned to a station marker while "following",
  which conflicted with following the viewer's own position.

### 2026-09-02 - Phase 3: live map

#### Added
- `web.py` - FastAPI server. Role token in the URL path gates every route; an
  invalid token returns 404 rather than 403, so it cannot confirm that an event
  exists. Endpoints: the map page, a full state snapshot, and a WebSocket feed.
- `access.py` - role tokens (`ncs`, `liaison`). No user accounts: one long
  random URL per role, pasted into the right group text. Tokens are scoped to
  their event and can be revoked. NCS writes, Liaison is read-only.
- `hub.py` - in-process per-event fan-out. One APRS-IS connection feeds every
  browser. Subscribers have bounded queues: a stalled client drops messages
  rather than stalling the ingest loop, which is safe because clients resync
  full state on reconnect.
- `static/` - the map client. Leaflet, no build step. Courses drawn in
  `sort_order`, POIs with What3Words in popups, live station markers.
- Layer toggles with role defaults - Liaison starts with aid station operators
  hidden, keeping a phone screen readable. Preferences persist per browser.
- Station list sorted with whatever needs attention first: silent, then stale,
  then fresh, with non-beaconing operators last.
- Browser geolocation for "where am I" - local only, never transmitted.
- CLI: `serve`, `links`, `list-links`, `revoke-link`.
- Schema: `access_token` table.
- 21 tests (108 total) covering access control, state shape, and fan-out.

#### Fixed
- The connection badge was hidden behind Leaflet's zoom control, which sits in
  the same corner at a higher z-index. Found by rendering the page in a real
  browser; the top bar now reserves space for it. This mattered because the
  badge is the only signal that a phone is showing stale data.
- `Subscription` was unhashable: `@dataclass` generates `__eq__`, which unsets
  `__hash__`, and subscriptions live in a set. Value equality would also have
  collapsed two browsers on the same event into one subscriber.
- Replaced deprecated FastAPI `on_event` handlers with a lifespan context.

#### Client rules worth keeping
- Marker positions are never interpolated. Reports arrive every 1-5 minutes;
  animating between them would show a position that was never reported.
- Ages redraw on a timer so "2m ago" does not sit there reading 2m forever.
- Reconnect resyncs full state rather than replaying messages.

#### Verified in a browser
- The real 26.4 mi Mankato course renders over OSM tiles with start and finish
  markers, correct colours, and working layer toggles.
- Station shapes and status colours: a fresh sweep (green square) and a stale
  SAG (amber diamond) are distinguishable by shape as well as colour.
- The bottom sheet slides over a full-bleed map. A true 375px render could not
  be checked - window resizing was unavailable - so the narrow layout is
  verified by forcing the mobile stylesheet, not by a real phone viewport.

### 2026-09-02 - styleUrl disambiguation (found with real data)

Validated the importer against a real MapMyRun export of the 2026 Mankato
Marathon course: 1415 points, 26.40 mi measured against an official 26.22.

#### Added
- `KmlFeature.style_id` and an `import_feature.style_id` column, capturing
  `<styleUrl>`. Exporters routinely give several placemarks the SAME name and
  distinguish them only by style - MapMyRun names the start marker, the finish
  marker and the route itself all after the route. Without this the start and
  finish were indistinguishable in the review list, both falling back to
  `unassigned`.
- `suggest()` now considers `style_id`, correctly proposing `poi:start` and
  `poi:finish` for those markers.
- `review` prints the style whenever a name is not unique in the listing.
- 5 tests covering the MapMyRun shape (87 total).

#### Fixed
- Hint patterns never matched inside `start_marker`: `_` is a word character, so
  a ``-anchored pattern cannot match `start` there. Separators are now
  normalized to spaces before matching.

#### Verified against real data
- Dedupe removed 157 of 1415 points - MapMyRun repeats the vertex at every
  routing-segment join, which is what `dedupe_consecutive` exists for.
- 13 segment gaps over 200 m (largest 1241 m) where the route builder used
  direct/offroad mode instead of snapping to roads. The line therefore cuts
  corners, which matters for Phase 5 mile computation.
- Geometry extracted from the MapMyRun HTML page is byte-identical to the
  official KML export, so either source is usable.
- The export contains no aid stations - only the route, a start and a finish.
  Aid station locations must come from elsewhere regardless of file format.

### 2026-09-02 - Course styling and draw order

#### Added
- `styling.py` - course color and line-style handling. Colors come from the
  Okabe-Ito colorblind-safe palette minus the yellow, which vanishes against
  light map tiles; a new course takes the next unused one automatically.
- Adjustable draw order per course (`course.sort_order`). Where the Full, Half
  and 10K share road their lines are coincident, and draw order decides which
  one is visible. This is the primary control for overlap and will be adjustable
  in the UI.
- `course.dash_pattern` - opt-in line styles (solid, long, dotted, dash-dot,
  medium, or a raw SVG dasharray). Courses are solid by default; a dash is for
  the case draw order cannot cover, seeing two coincident routes at once.
- CLI `style-course` to change a course's color, line style, name or draw order.
  `assign-course` gained `--dash`, and `courses` now lists draw order and style.
- A column migration step in `init_schema`, applied only where missing.
- 17 new tests (82 total).

#### Fixed
- New columns would never have reached an existing database: `CREATE TABLE IF
  NOT EXISTS` silently skips a table that already exists, so `poi.what3words`
  and `course.dash_pattern` were unreachable on any file created earlier.
  `init_schema` now adds missing columns and reports what it applied.

### 2026-09-02 - Phase 2: KML/KMZ import

#### Added
- MIT license.
- `geo.py` - haversine distance, polyline length, consecutive-point dedupe, and
  segment stitching. Coordinates are (lon, lat) throughout, matching GeoJSON/KML.
- `kml.py` - KML/KMZ parsing built around the defects organizer files actually
  have: namespace variation, deep Document/Folder nesting, MultiGeometry,
  meaningless placemark names, coordinates with newlines and altitudes. Folder
  path is retained because it is often the only clue to what a feature is.
  Classification is advisory only.
- `importer.py` - two-phase import. Files stage into `import_feature` as
  `pending`; a human assigns each to a course or POI. Additive across files.
- Schema: `import_batch` and `import_feature` staging tables.
- CLI: `import`, `review`, `assign-course`, `assign-poi`, `discard`, `courses`,
  `set-w3w`.
- `tests/fixtures/messy_course.kml` - fixture reproducing real-world defects:
  a course split across segments with one drawn backwards, an "Untitled Path",
  a folder mixing aid stations with parking, a MultiGeometry, and a point with
  no altitude.
- 41 new tests (65 total), including hardening tests for entity expansion, XXE
  and zip bombs.

#### Changed
- Added `defusedxml` as a second runtime dependency. KML arrives from race
  organizers and will be uploaded through the web UI, making it untrusted
  third-party input; the stdlib XML parser does not guard entity expansion.
- KMZ archives are checked for decompression bombs and size-capped, which
  defusedxml does not cover since it only protects the XML parse.
- CLI output is ASCII-only. Em dashes rendered as mojibake in the Windows
  console, and a club laptop is the target environment.

#### Fixed
- `geo.stitch` grew the chain only from the tail, so a file listing a middle
  segment first got the front piece reversed onto the back, folding the course
  over itself. It now grows at both ends. This was a silent, plausible-looking
  corruption of a real course.

### 2026-09-02 — Phase 1: APRS-IS ingest

#### Added
- Project scaffold: `pyproject.toml` (src layout, `awt` console script), `.gitignore`,
  `.env.example`, venv-based dev setup. Single runtime dependency: `aprslib`.
- `schema.sql` — full domain schema. Every table carries `event_id` so one database
  file can host multiple events without restructuring: `event`, `course`, `poi`,
  `roster`, `position`, `raw_packet`.
- `parser.py` — raw APRS text to `PositionReport`. Delegates decoding to `aprslib`,
  which covers uncompressed, base-91 compressed, Mic-E and NMEA encodings. Rejects
  non-position packets and Null Island (0,0) with a typed `Rejected` reason.
- `aprsis.py` — async APRS-IS client. One connection for the whole application,
  receive-only login (passcode `-1`), roster-derived buddy filter, 120s read timeout,
  exponential backoff with jitter capped at 300s. Login line is never logged.
- `db.py` — SQLite access layer. WAL mode, foreign keys on, autocommit.
- `ingest.py` — feed to parse to store, with an `on_position` callback that is the
  seam the Phase 3 WebSocket fan-out will plug into.
- `units.py` — metric storage, US customary presentation. Speed in mph, altitude in
  feet, course distance in miles.
- `what3words.py` — normalize, shape-validate and format W3W addresses. No API calls.
- `cli.py` — `awt init-db | add-event | add-station | roster | ingest | tail`.
- Test suite: 24 tests over a fixture corpus of APRS packet encodings, unit
  conversion, filter construction and roster semantics. No network required.
- `docs/PLAN.md` — full project plan, phase detail, domain decisions and risks.
- What3Words field on `poi`, maintained by NCS, entered by hand.

#### Fixed
- Ingest was discarding packets from rostered operators marked `expects_aprs=0`.
  The APRS-IS filter should request only stations expected to beacon, but the
  membership check must accept the whole roster — an aid station operator who
  turns a tracker on mid-event is still one of ours. Filter construction and
  roster membership are now separate queries (`tracked_station_keys` vs
  `all_station_keys`).

#### Verified
- `aprslib` 0.7.2 parses uncompressed, compressed and Mic-E packets correctly on
  Python 3.13, and normalizes speed to km/h and altitude to meters.
- End-to-end: event creation, roster with a non-beaconing aid station, filter
  generation excluding it, fixture packets through the real ingest handler, and
  `tail` rendering stored positions in mph/feet.
