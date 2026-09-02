# AprsWebTracker

Web map for marathon-style events: race courses and aid stations from organizer
KML/KMZ, overlaid with live APRS positions of the ham radio operators supporting
the event. Built to be stood up by a radio club without much effort.

Full plan and phase detail: **`docs/PLAN.md`**. Complete history: **`CHANGELOG.md`**.

## Status

Phase 1 (APRS-IS ingest) is complete. No web server or UI exists yet.

Phases: 1 ingest ✅ · 2 KML import · 3 live map · 4 roster/NCS panel ·
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
  units.py        metric storage -> US customary display
  what3words.py   normalize/validate W3W strings, no API
  cli.py          awt entry point
tests/fixtures/packets.txt   packet corpus, `expectation|raw` per line
docs/PLAN.md                 the plan
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

- **2026-09-02** Fixed: ingest discarded packets from rostered operators marked
  `expects_aprs=0`; filter and membership are now separate queries.
- **2026-09-02** Added `what3words.py` and a `poi.what3words` column, NCS-maintained,
  manual entry, no API.
- **2026-09-02** Added `docs/PLAN.md` with full phase detail and domain decisions.
- **2026-09-02** Added `cli.py`: `init-db`, `add-event`, `add-station`, `roster`,
  `ingest`, `tail`.
- **2026-09-02** Added test suite (24 tests) over a packet fixture corpus; no
  network required.
- **2026-09-02** Added `units.py` — metric storage, US customary presentation.
- **2026-09-02** Added `ingest.py` with the `on_position` hook for the Phase 3
  WebSocket fan-out.
- **2026-09-02** Added `aprsis.py` — async client, receive-only login, buddy filter,
  backoff with jitter.
- **2026-09-02** Added `parser.py` and `db.py`; verified aprslib handles Mic-E,
  compressed and uncompressed on Python 3.13.
- **2026-09-02** Added project scaffold and `schema.sql` with the full event-scoped
  domain model.
