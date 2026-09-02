# AprsWebTracker

Web map for marathon-style events: race courses and aid stations from organizer
KML/KMZ, overlaid with live APRS positions of the ham radio operators supporting
the event. Built to be stood up by a radio club without much effort.

Full plan and phase detail: **`docs/PLAN.md`**. Complete history: **`CHANGELOG.md`**.

## Status

Phases 1 (APRS-IS ingest) and 2 (KML/KMZ import) are complete, driven from the
CLI. No web server or UI exists yet. Repo: private, `MikeWills/AprsWebTracker`.

Phases: 1 ingest ✅ · 2 KML import ✅ · 3 live map · 4 roster/NCS panel ·
4a What3Words · 5 course-relative position · 6 incidents · 7 replay · 8 deployment

## Commands

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e ".[dev]"   # Windows
cp .env.example .env                                    # then set APRS_CALLSIGN

./.venv/Scripts/python.exe -m pytest -q                 # 24 tests, no network

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
awt set-w3w marathon2026 4 index.home.raft
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
  what3words.py   normalize/validate W3W strings, no API
  cli.py          awt entry point
tests/fixtures/packets.txt        packet corpus, `expectation|raw` per line
tests/fixtures/messy_course.kml   KML reproducing real organizer defects
docs/PLAN.md                      the plan
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
- **CLI output stays ASCII.** Em dashes become mojibake in the Windows console,
  and a club laptop is the target environment.

## Conventions

- Python 3.11+, `from __future__ import annotations`, dataclasses for value types.
- Dependencies stay minimal — every added package is one more thing a club has to
  install. One runtime dependency today (`aprslib`); justify any second one.
- Comments explain *why*, especially where a choice looks arbitrary but is
  protecting against a real event-day failure.
- New parser edge cases get a line in `tests/fixtures/packets.txt`, not a bespoke
  test. Captured live traffic goes in `live_*.txt`, which is gitignored — it holds
  the positions of people who did not consent to a public repo.

## Recent changes

Last 10 entries; full record in `CHANGELOG.md`.

- **2026-09-02** Fixed: `geo.stitch` folded a course back on itself when the file
  listed a middle segment first; the chain now grows at both ends.
- **2026-09-02** CLI output made ASCII-only for the Windows console.
- **2026-09-02** Added `defusedxml`; KMZ decompression-bomb and size guards, with
  entity-expansion, XXE and zip-bomb tests.
- **2026-09-02** Added `importer.py` and the `import_batch`/`import_feature`
  staging tables — two-phase import, additive across files.
- **2026-09-02** Added `kml.py` and `geo.py`, plus `messy_course.kml` reproducing
  real organizer defects.
- **2026-09-02** Added import CLI: `import`, `review`, `assign-course`,
  `assign-poi`, `discard`, `courses`, `set-w3w`.
- **2026-09-02** Added MIT license; pushed to private repo `MikeWills/AprsWebTracker`.
- **2026-09-02** Fixed: ingest discarded packets from rostered operators marked
  `expects_aprs=0`; filter and membership are now separate queries.
- **2026-09-02** Added `what3words.py` and a `poi.what3words` column, NCS-maintained,
  manual entry, no API.
- **2026-09-02** Phase 1 complete: `parser.py`, `aprsis.py`, `db.py`, `ingest.py`,
  `units.py`, `cli.py`, `schema.sql`, and `docs/PLAN.md`.
