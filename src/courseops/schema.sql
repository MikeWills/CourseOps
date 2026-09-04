-- Course Ops schema.
-- Every domain table carries event_id so a single database file can host
-- multiple events (and, later, multiple clubs) without restructuring.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS event (
    id                INTEGER PRIMARY KEY,
    -- The club that owns this event. Forward reference: SQLite resolves
    -- foreign keys lazily, so the table order here does not matter.
    organization_id   INTEGER REFERENCES organization(id) ON DELETE CASCADE,
    slug              TEXT    NOT NULL UNIQUE,
    name              TEXT    NOT NULL,
    event_date        TEXT,
    timezone          TEXT    NOT NULL DEFAULT 'UTC',
    center_lat        REAL,
    center_lon        REAL,
    zoom              INTEGER NOT NULL DEFAULT 13,
    -- Appended to the roster-derived buddy filter, e.g. 'r/34.73/-86.58/30'
    -- to also pick up un-rostered stations near the course.
    aprs_filter_extra TEXT,
    is_active         INTEGER NOT NULL DEFAULT 1,
    -- Whether this event's APRS-IS feed should be running.
    --
    -- Persisted rather than held in memory because a deploy restarts the
    -- service, and a feed that quietly failed to come back during an event
    -- would look exactly like a quiet net. Off by default: outside race day
    -- the filter matches each operator's callsign wherever they are, so
    -- running it continuously would log where volunteers live and work.
    ingest_enabled    INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Race courses (Full / Half / 10K), imported from organizer KML/KMZ. Phase 2.
CREATE TABLE IF NOT EXISTS course (
    id         INTEGER PRIMARY KEY,
    event_id   INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    color        TEXT,
    dash_pattern TEXT,
    -- The colour of this race's bibs. Usually matches the course line colour,
    -- which is why it defaults to it - but it is a separate field because the
    -- two answer different questions: the line colour is a map choice, the bib
    -- colour is how an aid station operator identifies a runner in front of
    -- them and says "first yellow male just went through".
    bib_color    TEXT,
    bib_color_name TEXT,
    geojson    TEXT    NOT NULL,
    distance_m REAL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

-- Fixed locations: aid stations, start/finish, medical. Ground truth from KML,
-- and exists whether or not anyone there is beaconing APRS.
CREATE TABLE IF NOT EXISTS poi (
    id       INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    name     TEXT    NOT NULL,
    poi_type TEXT    NOT NULL DEFAULT 'aid_station',
    lat      REAL    NOT NULL,
    lon      REAL    NOT NULL,
    -- What3Words address, maintained by NCS. Aid stations sit at intersections
    -- and park entrances where a street address is useless and a lat/lon is
    -- painful to read over voice. Entered by hand: the W3W API needs a key and
    -- carries licensing terms, which is a poor trade for a field that changes
    -- once per event. Stored as given; validated only loosely.
    what3words TEXT,
    -- One or two characters drawn on the pin, so a glance answers "which one
    -- is that?" without opening a popup. Almost always NULL: the label is
    -- derived from the name (see labels.py), and this holds an override for
    -- the places where that guess comes out wrong or collides with another.
    label    TEXT,
    -- The club's own running order, when geometry cannot work it out.
    --
    -- An event with three routes has three sequences of stops sharing one
    -- flat list, and each stop is snapped to whichever course line is nearest
    -- - a coin flip where routes share pavement. The result interleaves miles
    -- measured on different races into one meaningless order. Which stop
    -- follows which is a fact the club holds and the geometry does not.
    --
    -- 0 means "not placed by hand": those sort last, by course distance, so an
    -- event that has never been ordered behaves exactly as before and a newly
    -- imported place lands at the end rather than jumping into the middle.
    sort_order INTEGER NOT NULL DEFAULT 0,
    notes    TEXT
);

-- Which races each place serves.
--
-- Not a column on `poi`, because one water stop routinely serves several
-- races: the organizer's own file says so in the names - "WATER (ALL)",
-- "MM 15 (FULL)". A single course_id would force a club to duplicate a place
-- per race, and then a rename or a What3Words address would have to be typed
-- three times and would drift.
--
-- No rows for a place means "not stated", and the app falls back to snapping
-- it onto the nearest course line. That fallback is a guess - the lines share
-- pavement - and it is exactly what this table exists to replace, but it keeps
-- an event that predates this working unchanged.
CREATE TABLE IF NOT EXISTS poi_course (
    event_id  INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    poi_id    INTEGER NOT NULL REFERENCES poi(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
    PRIMARY KEY (poi_id, course_id)
);

CREATE INDEX IF NOT EXISTS idx_poi_course_course
    ON poi_course (event_id, course_id);

-- What kinds of place this event has, and how each is drawn.
--
-- Deliberately data rather than an enum in the code. A KML arrives with
-- whatever layers the organizer drew - mile markers, medical, aid stations,
-- traffic control, portable toilets, spectator zones - and the next club will
-- have a different set. Hardcoding the list means editing Python to accept a
-- race, which is the opposite of what this project is for. There is no limit
-- on how many a club may add.
--
-- `key` is the stable identifier stored in poi.poi_type; `name` is the club's
-- own wording and may be renamed freely without breaking anything that keys
-- off the category.
CREATE TABLE IF NOT EXISTS poi_category (
    id         INTEGER PRIMARY KEY,
    event_id   INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    key        TEXT    NOT NULL,
    name       TEXT    NOT NULL,
    -- "We put a person here and track them." This is the flag that decides
    -- which layers get the operational treatment - an operator posted to them,
    -- a lead runner sighted at them, a What3Words address worth maintaining.
    -- A mile marker or a portable toilet is a layer you turn on and off; an
    -- aid station is somewhere a human is standing.
    staffed    INTEGER NOT NULL DEFAULT 0,
    -- Name from the shared glyph set; see static/icons.js.
    icon       TEXT    NOT NULL DEFAULT 'pin',
    color      TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    -- Whether the layer starts switched on. A club with a 26-marker mile layer
    -- will want it off by default; aid stations always on.
    visible    INTEGER NOT NULL DEFAULT 1,
    -- Whether pins in this layer carry their label. Per layer, not global:
    -- a course has a dozen aid stations worth labelling and fifty mile
    -- markers that would turn the map into a wall of digits.
    show_labels INTEGER NOT NULL DEFAULT 0,
    UNIQUE (event_id, key)
);

CREATE INDEX IF NOT EXISTS idx_poi_category_event
    ON poi_category (event_id, sort_order);

-- The kinds of job a person does at this event.
--
-- Open, like the place layers: a club fields roles the defaults do not have -
-- Liaison being the obvious one, the operator embedded with Public Safety -
-- and a fixed list meant editing Python to accept them.
--
-- The seven defaults each carry their own status vocabulary in
-- db.OP_STATUS_LABELS, because an aid station is "Torn down" where a sweep is
-- "Finished". A role a club adds has no entry there and falls back to the
-- generic "Not started / Active / Closed", which is the right trade.
--
-- `key` is the stable identifier stored in roster.category; `name` is the
-- club's own wording and may be renamed freely.
CREATE TABLE IF NOT EXISTS roster_role (
    id       INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    key      TEXT    NOT NULL,
    name     TEXT    NOT NULL,
    -- The set is the club's, so the order cannot come from a list in the code.
    sort_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (event_id, key)
);

-- Assigned operators. NOT the same set as "stations sending APRS": most aid
-- station operators never beacon. See expects_aprs.
CREATE TABLE IF NOT EXISTS roster (
    id            INTEGER PRIMARY KEY,
    event_id      INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    -- SSID-qualified identity. 'N0CALL-9' and 'N0CALL-7' are different radios
    -- on different people and must never be merged.
    station_key   TEXT    NOT NULL,
    -- The SSID actually heard on the air, when station_key names only a
    -- callsign. Volunteers know their callsign; the SSID is a property of
    -- whichever radio or phone app they bring on the day, and asking a
    -- coordinator to collect it in advance is asking to be told the wrong one.
    -- So the roster may hold a bare callsign and this is filled in from the
    -- first packet that looks like a person rather than infrastructure.
    -- NULL means either nothing heard yet, or the roster named the SSID
    -- outright. Kept separate from station_key so what a human typed is never
    -- silently rewritten, and so a mis-bind can be undone.
    bound_key     TEXT,
    operator_name TEXT,
    display_label TEXT    NOT NULL,
    -- net_control | aid_station | sweep | sag | shadow | rover | start_finish
    category      TEXT    NOT NULL DEFAULT 'rover',
    -- Only stations with expects_aprs=1 are subject to staleness alerting.
    -- Without this the "who has gone quiet" panel fills with operators who
    -- were never going to beacon, and stops being read.
    expects_aprs  INTEGER NOT NULL DEFAULT 1,
    -- Fixed assignment: drawn at this POI when they do not beacon.
    poi_id        INTEGER REFERENCES poi(id) ON DELETE SET NULL,
    -- Manual, NCS-set. pending | active | closed. Independent of radio status:
    -- "on station, no APRS" is healthy, "rolling, silent 18 min" is an alarm.
    op_status     TEXT    NOT NULL DEFAULT 'pending',
    -- When op_status last changed, and who said so. The timestamp answers
    -- "closed at 10:42" on the after-action; the initials are a log annotation
    -- for shift handover, not authentication.
    op_status_at  TEXT,
    op_status_by  TEXT,
    color         TEXT,
    UNIQUE (event_id, station_key)
);

CREATE TABLE IF NOT EXISTS position (
    id           INTEGER PRIMARY KEY,
    event_id     INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    station_key  TEXT    NOT NULL,
    received_at  TEXT    NOT NULL,
    lat          REAL    NOT NULL,
    lon          REAL    NOT NULL,
    course_deg   REAL,
    speed_kmh    REAL,
    altitude_m   REAL,
    symbol_table TEXT,
    symbol_code  TEXT,
    comment      TEXT,
    aprs_format  TEXT,
    raw          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_position_station
    ON position (event_id, station_key, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_position_time
    ON position (event_id, received_at DESC);

-- Every line the feed delivers, parsed or not. Cheap at this traffic volume,
-- and the source of both post-event replay and new parser test fixtures.
CREATE TABLE IF NOT EXISTS raw_packet (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER REFERENCES event(id) ON DELETE CASCADE,
    received_at TEXT    NOT NULL,
    raw         TEXT    NOT NULL,
    -- stored | no_position | not_rostered | parse_error
    status      TEXT    NOT NULL,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_raw_packet_time ON raw_packet (received_at DESC);

-- KML/KMZ import staging. Parsed features land here unassigned; a human
-- classifies each one before it becomes a course or a POI. Organizer files are
-- messy enough that a silent importer costs more time than the review step does,
-- so nothing reaches `course` or `poi` without being confirmed.
CREATE TABLE IF NOT EXISTS import_batch (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    filename    TEXT    NOT NULL,
    source_kind TEXT    NOT NULL,  -- kml | kmz
    imported_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE TABLE IF NOT EXISTS import_feature (
    id          INTEGER PRIMARY KEY,
    batch_id    INTEGER NOT NULL REFERENCES import_batch(id) ON DELETE CASCADE,
    event_id    INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    folder      TEXT,
    geom_type   TEXT    NOT NULL,  -- linestring | point | polygon
    geojson     TEXT    NOT NULL,
    length_m    REAL,
    description TEXT,
    -- <styleUrl> reference. Sometimes the only thing distinguishing two
    -- placemarks that share a name (MapMyRun names start and finish alike).
    style_id    TEXT,
    warnings    TEXT,
    suggestion  TEXT,               -- advisory guess; never applied on its own
    -- pending | assigned | discarded
    status      TEXT    NOT NULL DEFAULT 'pending',
    course_id   INTEGER REFERENCES course(id) ON DELETE SET NULL,
    poi_id      INTEGER REFERENCES poi(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_import_feature_event
    ON import_feature (event_id, status);

-- Role-based access. v1 has no user accounts: each event gets one long random
-- URL per role, pasted into the right group text. It is a bearer token, exactly
-- as secure as that message - appropriate for this data, and it lets a
-- volunteer whose phone died be re-admitted by re-sending a link.
CREATE TABLE IF NOT EXISTS access_token (
    id         INTEGER PRIMARY KEY,
    event_id   INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    token      TEXT    NOT NULL UNIQUE,
    role       TEXT    NOT NULL,   -- ncs | liaison
    label      TEXT,
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_used  TEXT
);

CREATE INDEX IF NOT EXISTS idx_access_token_event ON access_token (event_id, role);

-- Runner pickups and other incidents NCS is tracking.
--
-- Modelled as an incident, not a pin: the pin is only how it is drawn. A pin
-- that can be only "there" or "gone" does not survive a real event, because the
-- question that matters is "this was requested eight minutes ago and nobody has
-- been dispatched" - which needs a status and a clock, not a marker.
--
-- Deliberately NOT stored: any description of a runner's condition. Bib,
-- location, status and a short operational note are enough to run the net.
-- Inviting narrative medical detail would make this a system holding health
-- information about identifiable people, which changes our obligations and the
-- organizer's. The bib is the organizer's identifier; we never hold the
-- bib-to-name mapping and should not want it.
CREATE TABLE IF NOT EXISTS incident (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    -- The organizer's runner identifier. Often unknown when first called in, so
    -- nullable and fillable later.
    bib         TEXT,
    -- pickup | note. A pickup is a dispatch problem with a workflow; a note is
    -- a record of something the organizer should know afterwards (a blocked
    -- intersection, a confusing turn). Notes must never appear in the pickup
    -- queue: that queue is read as "who is still waiting".
    kind        TEXT    NOT NULL DEFAULT 'pickup',
    -- reported | en_route | picked_up | dropped_off | closed
    status      TEXT    NOT NULL DEFAULT 'reported',
    lat         REAL    NOT NULL,
    lon         REAL    NOT NULL,
    -- Set when reported "at Aid 4" rather than by dropping a pin.
    poi_id      INTEGER REFERENCES poi(id) ON DELETE SET NULL,
    note        TEXT,
    assigned_to TEXT,
    reported_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    reported_by TEXT,
    -- When the CURRENT status began. This is what "waiting 8 minutes" is
    -- measured from, and what the list sorts on.
    status_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    status_by   TEXT,
    closed_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_incident_event ON incident (event_id, status, status_at);

-- Every change, for the after-action review and for shift handover. Answers
-- "who marked this picked up, and when".
CREATE TABLE IF NOT EXISTS incident_log (
    id          INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incident(id) ON DELETE CASCADE,
    at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    by          TEXT,
    action      TEXT    NOT NULL,   -- created | status | edited
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_incident_log ON incident_log (incident_id, at);


-- Lead runner sightings, called in from aid stations.
--
-- The counterpart to the sweep: the sweep says when an aid station can close,
-- the leader says when it has to be ready. We only ever learn this when a
-- runner physically passes an operator who reports it on the net, so this is a
-- log of reports, not a track. Current position is derived from the latest
-- sighting rather than stored, which keeps the two from disagreeing and gives
-- pace for free.
CREATE TABLE IF NOT EXISTS lead_sighting (
    id        INTEGER PRIMARY KEY,
    event_id  INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    course_id INTEGER NOT NULL REFERENCES course(id) ON DELETE CASCADE,
    -- male | female | any other division a race tracks. Stored as text so a
    -- club can add wheelchair or non-binary without a migration.
    division  TEXT    NOT NULL,
    poi_id    INTEGER NOT NULL REFERENCES poi(id) ON DELETE CASCADE,
    bib       TEXT,
    at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    by        TEXT
);

CREATE INDEX IF NOT EXISTS idx_lead_sighting
    ON lead_sighting (event_id, course_id, division, at DESC);

-- Every operational status change, never overwritten.
--
-- `roster.op_status` holds only the current value, so without this the sequence
-- is lost the moment it is overwritten - and it cannot be recovered afterwards.
-- Two reasons to keep it:
--
--   1. Shift handover. "Aid 4 closed at 11:32, reopened at 11:40 by AB" is
--      exactly what an incoming NCS operator needs, and the roster row alone
--      cannot say it.
--   2. Any after-action question about the timeline, including replay if it is
--      ever built (issue #2). History has to be captured while it happens.
CREATE TABLE IF NOT EXISTS roster_status_log (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    station_key TEXT    NOT NULL,
    at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    by          TEXT,
    from_status TEXT,
    to_status   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_roster_status_log
    ON roster_status_log (event_id, station_key, at);

-- SSIDs to keep OFF the map, under a callsign we otherwise want.
--
-- The live filter is a wildcard per rostered callsign by default, because a
-- volunteer who signs up as WX0MIK-1 and beacons WX0MIK-5 would otherwise be
-- silently invisible on race morning - and a missing person is far worse than
-- an extra marker. The cost of that default is the operator's own digipeater,
-- igate or home station arriving too. This is how they are dismissed: once,
-- before the event, by name.
CREATE TABLE IF NOT EXISTS station_exclusion (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    station_key TEXT    NOT NULL,
    reason      TEXT,
    added_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (event_id, station_key)
);

-- Server-wide setup access.
--
-- Separate from access_token, which is scoped to one event: creating the FIRST
-- event needs a token that cannot belong to an event yet. Printed when the
-- server starts.
--
-- This is the most powerful credential the app has - it can read and change
-- every event - so it is deliberately not something a club circulates. One
-- person sets up; everyone else gets a role link.
CREATE TABLE IF NOT EXISTS admin_token (
    id         INTEGER PRIMARY KEY,
    token      TEXT    NOT NULL UNIQUE,
    label      TEXT,
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_used  TEXT
);

-- Administrator accounts.
--
-- Only administrators have accounts. Volunteers on the day still use role
-- links, deliberately: a link can be re-sent to someone whose phone died, at
-- 6am, by anyone holding it - no account recovery, no admin awake to do it.
-- Setup is different work: it happens beforehand, by a named person, and needs
-- to be attributable.
CREATE TABLE IF NOT EXISTS user (
    id            INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL UNIQUE,
    -- scrypt, self-describing: `scrypt$n$r$p$salt$hash`. The cost parameters
    -- travel with each hash so they can be raised without invalidating
    -- existing passwords.
    password_hash TEXT    NOT NULL,
    -- system_admin | event_admin
    role          TEXT    NOT NULL,
    -- The club this administrator belongs to. NULL for a system administrator,
    -- who is not part of any one club.
    organization_id INTEGER REFERENCES organization(id) ON DELETE CASCADE,
    display_name  TEXT,
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_login    TEXT
);

-- Which events an event_admin may manage. System admins ignore this table.
-- This is what keeps one club's officer out of another club's event once this
-- is hosted for more than one club.
CREATE TABLE IF NOT EXISTS user_event (
    user_id  INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    event_id INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, event_id)
);

-- Login sessions. Kept in the database rather than a signed cookie so they can
-- actually be revoked - on password change, on deactivation, on logout.
CREATE TABLE IF NOT EXISTS session (
    token      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    expires_at TEXT    NOT NULL,
    last_used  TEXT
);

CREATE INDEX IF NOT EXISTS idx_session_user ON session (user_id);

-- A club or organizing body. The tenancy boundary.
--
-- Added so this can be hosted for several clubs: an organization creates and
-- runs its own events without a system administrator in the loop, and cannot
-- see anyone else's. Every event belongs to exactly one.
CREATE TABLE IF NOT EXISTS organization (
    id         INTEGER PRIMARY KEY,
    slug       TEXT    NOT NULL UNIQUE,
    name       TEXT    NOT NULL,
    contact    TEXT,
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
