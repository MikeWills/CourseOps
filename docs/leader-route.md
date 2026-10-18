# Leader route (#210)

Design for stating, per race, exactly which stops the lead runner passes and in
what order. Written before the code because it needs a schema migration and
the rule is that a migration has a rollback path on paper before it ships.

## The case that forces it

Mankato, 2026-10-17. The Full goes out to stop J and comes back by stop I:

    H -> I -> J -> I -> K

The leader passes I twice. Today (read from `leaders.py`, not run):

- A sighting is "leader was at stop I". Nothing says which pass.
- "The next station" is the stop after the FIRST I in the club's order, so after
  the second I the panel says the next stop is J again, not K.
- Each stop has one distance per race, so the legs I->J and J->I produce no
  pace and no ETA (the second leg runs backwards).

`poi.sort_order` is one order for the whole event, and `poi_course` is a set of
ticks. Neither can say "I, twice" or "the 10K skips F".

## Model

New table `course_stop`: the route, one row per step.

    course_stop (
        id        INTEGER PRIMARY KEY,
        event_id  INTEGER NOT NULL REFERENCES event(id)  ON DELETE CASCADE,
        course_id INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
        step      INTEGER NOT NULL,                    -- 0, 1, 2 ... within the race
        poi_id    INTEGER NOT NULL REFERENCES poi(id)  ON DELETE CASCADE,
        UNIQUE (course_id, step)
    )

- A place may appear in several steps of one race. That is the point.
- The finish is just the last step (usually the finish place), which is what
  lets #215 ask for an ETA to it.
- No rows for a race means "not stated": everything behaves exactly as today.
  Same shape as `poi_course` and for the same reason, so an event that predates
  this keeps working and nothing is guessed into the route.
- Deleting a place that a route uses cascades its steps away. Setup must say so
  and refuse with a count, like deleting a layer that has places.
- The route is club configuration, so `course_stop` goes in
  `admin.EXPORTED_TABLES` (a test fails otherwise).

`lead_sighting` gets a nullable `step INTEGER` (added to `db._ADDED_COLUMNS`
and `schema.sql`). A sighting with no step (every one recorded before this)
is matched to a step by place, first pass, as today.

## Behaviour

1. **Setup.** On the Route stops tab, per race, an ordered list: add a stop, drag to
   reorder, remove a step, one Save-all (`bindSaveAll`). A "Fill from ticked
   stops" button drafts a route from `poi_course` in the club's order. It is a
   draft shown for review, never saved on its own. Repeats are added by adding
   the same stop again.
2. **Passed button.** "Passed [next]" records the next step of the route and
   stores its `step`. The picker still lists every stop (a correction may name a
   stop off the route), and recording a place off the route stores `step` NULL.
3. **Next stop and pace.** Next stop = the step after the last sighting's step.
   Pace and ETA are measured step to step, so J->I is a normal leg.
4. **Distances.** `CourseIndex.progression` picks one pass per stop and returns
   a dict keyed by poi id, so it cannot hold I twice. A sibling,
   `route_progression(course_id, steps, stated)`, runs the same
   non-decreasing / least-offset search over the list of steps and returns a
   list indexed by step. The existing function stays for races with no route.
5. **Snapshot.** The leader payload gains `route` (step, poi id, name) and each
   sighting's `step`, so the client never rebuilds the order.

## Out of scope here

- Bib correction (#208), sweeps and the finish ETA (#215), relocation (#211).
  Each reads the steps this adds.
- Any change to how a place's mile is shown on the stations list.

## Delivery

- **PR A**: table, `step` column, setup API and UI, "Fill from ticked", export,
  guide. No change to what NCS sees. Safe to ship first.
- **PR B** (built): `route_progression`, leaders read the route, Passed follows
  steps, NCS shows the logged clock time. Behaviour change, no schema change.
  A step that does not match its place is stored as none, not refused.

## Rollback

Both schema changes are additive. Nothing existing is altered or dropped.

- **Code:** redeploy the previous tag
  (`sudo -u deploy .../deploy-release <older-tag>`). Older code never selects
  `course_stop` or `lead_sighting.step`, and SQLite ignores columns and tables
  it is not asked about, so the old release runs on the migrated database.
- **Data:** the pre-deploy snapshot (`deploy/backup.sh`, label `pre-deploy`)
  is the way back for the file itself. Routes entered after the deploy are
  lost on a snapshot restore; they are cheap to re-enter and, until PR B, change
  nothing anyone sees.
- **Not dropped on rollback:** leaving `course_stop` and the column in place is
  harmless, and preferable to a destructive down-migration.

## Tests (written first)

- Route with a repeated stop: H, I, J, I, K gives five steps, distances rising,
  both I's measured at their own pass.
- After a sighting of the second I the next stop is K, not J.
- Pace is positive on I->J and on J->I.
- A race with no route behaves exactly as before (existing leader tests pass
  unchanged).
- A sighting recorded before the migration (`step` NULL) still resolves.
- Deleting a place used in a route is refused with the count.
- `course_stop` is exported with the event.

## Open

- RESOLVED (#215, docs/sweep-tracking.md): the finish IS a step, the last one. No new concept.
- RESOLVED (Mike, 2026-10-17): one route per race. Everyone on a race, wheelchair
  field included, runs the same path past the same stops. The Half shares some
  of the Full's stops, skips some and adds others, which is why the route is
  per race and not one event-wide order.
