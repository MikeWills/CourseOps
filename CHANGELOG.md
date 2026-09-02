# Changelog

All notable changes to AprsWebTracker. Newest first. The ten most recent entries
are mirrored into `CLAUDE.md`; this file is the complete record.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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
