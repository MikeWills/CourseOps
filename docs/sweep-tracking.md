# Sweep tracking (#215)

Design for tracking the sweep past each stop, the time between stops, and an
estimate of when it reaches the finish. Written before the code because it adds
a column to an existing table and a migration needs its rollback on paper.

## Decision: a sweep is a leader of kind "sweep", not a new table

The issue asks whether to generalise `lead_sighting` or add a log. Generalise:

- A sweep passing is the same fact as a leader passing: "this runner/vehicle was
  at stop X at time T, reported on the net". Same Passed button, same undo and
  clear, same route steps (#210), same report table. A second append-only log
  would copy `record_sighting`, `undo`, `clear`, the NCS row and the report
  query, and the two would drift.
- The club already owns the list of things tracked (`lead_division`), and the
  taxonomy rule says the club's list is open. A sweep is a row in it. Multiple
  sweeps per race (a walking sweep and a vehicle) are therefore free: add two
  rows. The cost stays what the setup screen already states: one NCS row per
  race per leader.
- What a sweep needs that a leader does not is different pace limits and a
  finish ETA. That is a property of the row, so `lead_division` gains
  `kind TEXT NOT NULL DEFAULT 'leader'` (`'leader'` or `'sweep'`). Nothing
  keys on the NAME ("Sweep"), which the club may call anything.
- Not seeded for new events. Seeding would add a row per race to every NCS
  panel for clubs that never staff a sweep, and "never resurrect a deleted
  default" would then apply to it. The club adds it on the Leaders tab by
  ticking "This one is a sweep".

## Pace bounds are per kind

Rejecting an implausible pace stays (silence beats a wrong number). The bounds
are a function of kind, in one place (`leaders.pace_bounds`):

| kind   | fastest | slowest  |
|--------|---------|----------|
| leader | 3:00/mi | 30:00/mi |
| sweep  | 6:00/mi | 45:00/mi |

A walking sweep legitimately goes slower than 30:00/mile at the back, and
cannot be quicker than the last runner. A per-row editable limit was
considered and rejected: two more fields on the Leaders tab for a number nobody
has an opinion on until it is wrong.

## Leg times and the running pace

For a leader on a stated route, every consecutive pair of reports that both
resolve to route steps is a leg: stop, clock time, seconds since the previous
stop, metres along the route, leg pace (only if inside the kind's bounds).
Derived from the sightings on every read, never stored, so a correction or
undo changes them with no repair step. Without a stated route there is no
distance to measure and `legs` is empty: the old behaviour is unchanged.

`avg_pace_mps` is distance over time across the last three legs that have a
plausible pace, not the last one alone: the back of the pack is slow and noisy.
It must itself be inside the bounds or it is None. `pace_mps` (the last leg)
keeps its meaning.

## The finish is a step, not a special case (answers the open question in leader-route.md)

The route's last step IS the finish. No new column, no new concept: "Passed
Finish" is the same button recording the last step, and once the last step is
reported the leader is done (`finished`, no next, no ETA). `finish_eta_*` is
the remaining route distance from the latest report to the last step, divided
by `avg_pace_mps`. A club must end the route at the finish place; the guide
says so.

No ETA (all fields None) when: the route is not stated, there is one report,
the report is off the route, no plausible pace, the distance to the finish is
unknown or not positive, or the leader is already at the last step.

The server sends `finish_eta_at` (ISO UTC, last report plus remaining time) and
`finish_eta_seconds`. The browser shows the clock in the event's zone, 24-hour,
as everywhere else; "in 42 min" is derived in the browser from the clock, since
a relative figure decays. Labelled as an estimate from reports, not a tracker.

## Who sees it

The `leaders` array already goes to every role the snapshot sends it to
(Logistics, Liaison and Staff read it; NCS writes via `CAP_LEADERS`). Nothing
new to gate: the sweep rides the same payload, so Logistics sees the sweep's
clock times and finish estimate, read-only. No new mutation, so no new
capability.

## Report

The after-event table per race and leader gains a "Leg" column (time since the
previous stop). No names, times formatted by the browser in the event's zone.
The last row of a sweep's table is its finish time.

## Migration and rollback

`ALTER TABLE lead_division ADD COLUMN kind TEXT NOT NULL DEFAULT 'leader'`,
via `db._ADDED_COLUMNS` and `schema.sql`. Additive: every existing row becomes
a plain leader, which is what it was.

- Code: redeploy the previous tag. Older code reads `lead_division` by named
  columns it knows and ignores `kind`; SQLite is happy with the extra column.
  A row the club marked as a sweep reads as a leader on the old release.
- Data: the `pre-deploy` snapshot from `deploy/backup.sh` is the way back for
  the file. Sweep rows added after the deploy are cheap to re-add.
- No down-migration: leaving the column is harmless.
- No new table, so nothing new for `admin.EXPORTED_TABLES`; `lead_division`
  is already exported and `kind` travels with it.
