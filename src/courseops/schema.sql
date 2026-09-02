-- Course Ops schema.
-- Every domain table carries event_id so a single database file can host
-- multiple events (and, later, multiple clubs) without restructuring.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS event (
    id                INTEGER PRIMARY KEY,
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
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Race courses (Full / Half / 10K), imported from organizer KML/KMZ. Phase 2.
CREATE TABLE IF NOT EXISTS course (
    id         INTEGER PRIMARY KEY,
    event_id   INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    color        TEXT,
    dash_pattern TEXT,
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
    notes    TEXT
);

-- Assigned operators. NOT the same set as "stations sending APRS": most aid
-- station operators never beacon. See expects_aprs.
CREATE TABLE IF NOT EXISTS roster (
    id            INTEGER PRIMARY KEY,
    event_id      INTEGER NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    -- SSID-qualified identity. 'N0CALL-9' and 'N0CALL-7' are different radios
    -- on different people and must never be merged.
    station_key   TEXT    NOT NULL,
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
    -- reported | en_route | picked_up | closed
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
