"""Seed a demonstration event for the wiki screenshots.

Invented callsigns, operators and bibs on generated loops around Mankato;
no organizer file and no captured traffic, which is what keeps the
public-repo rule intact. Everything is timed relative to now, so the ages
on screen read as a race in progress whenever it is run.

    python tools/seed_demo.py demo.sqlite3
    DB_PATH=demo.sqlite3 APRS_CALLSIGN=N0DEM courseops serve riverbend2026 --no-ingest --port 8001

Sign in to /setup as demo / demo-demo-demo. The role links are printed.
The database is REPLACED if it exists.
"""
from __future__ import annotations
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from courseops import access, admin, categories, db, importer, incidents, leaders, users
from courseops.parser import PositionReport

if len(sys.argv) != 2:
    sys.exit("usage: seed_demo.py <path to database to create>")
DB = Path(sys.argv[1])
if DB.exists():
    DB.unlink()
conn = db.connect(DB)
db.init_schema(conn)

org_id = users.create_organization(conn, "riverbend", "Riverbend Amateur Radio Club")["id"]
users.create_user(conn, "demo", "demo-demo-demo", "system_admin",
                  display_name="Riverbend Admin", organization_id=org_id)

# Clean synthetic loops around Mankato: an outer Full, an inner Half sharing
# the Full's north side, a 10K inside that. Wobble so they read as roads.
C_LAT, C_LON = 44.163, -93.995
def ring(rx_km, ry_km, points, phase=0.0, wobble=0.12):
    out = []
    for k in range(points):
        t = 2 * math.pi * k / points
        w = 1 + wobble * math.sin(3 * t + phase) + 0.05 * math.cos(7 * t)
        lat = C_LAT + (ry_km * w / 111.0) * math.sin(t)
        lon = C_LON + (rx_km * w / (111.0 * math.cos(math.radians(C_LAT)))) * math.cos(t)
        out.append((round(lon, 6), round(lat, 6)))
    return out
full = ring(6.2, 4.6, 400)
n = len(full)
center = (C_LAT, C_LON)

ev = admin.create_event(conn, {"slug": "riverbend2026", "name": "Riverbend Marathon 2026",
                               "center_lat": center[0], "center_lon": center[1],
                               "timezone": "America/Chicago",
                               "event_date": "2026-10-17"}, organization_id=org_id)
event_id = ev["id"]

# Three loops sharing road: Full is the fixture, Half and 10K are prefixes
# closed with a straight leg back to the start.
def loop(points):
    return points + [points[0]]
courses = {
    "Full": loop(full),
    "Half": loop(ring(4.0, 3.0, 300, phase=1.0)),
    "10K":  loop(ring(2.2, 1.7, 200, phase=2.0)),
}
tmp = DB.parent / "demo_course.kml"
def placemark(name, coords):
    cs = " ".join(f"{lon},{lat},0" for lon, lat in coords)
    return f"<Placemark><name>{name}</name><LineString><coordinates>{cs}</coordinates></LineString></Placemark>"
tmp.write_text('<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
               + "".join(placemark(k, v) for k, v in courses.items())
               + "</Document></kml>", encoding="utf-8")
importer.stage_file(conn, event_id, tmp)
feats = {r["name"]: r["id"] for r in importer.pending_features(conn, event_id)}
course_ids = {}
for name, color, order in (("Full", "#c8321e", 30), ("Half", "#1a5fa5", 20), ("10K", "#d9a300", 10)):
    cid, _, _ = importer.assign_course(conn, event_id, [feats[name]], name=name, color=color)
    admin.update_course(conn, event_id, cid, {"sort_order": order})
    course_ids[name] = cid

# Places along the Full, by index into the line.
def at(i):
    lon, lat = full[i]
    return lat, lon
pois = {}
categories.add_poi_category(conn, event_id, "Water stops", staffed=False, icon="cup")
def place(name, key, i, courses_served, label=None):
    lat, lon = at(i)
    payload = {"name": name, "poi_type": key, "lat": lat, "lon": lon,
               "course_ids": [course_ids[c] for c in courses_served]}
    if label: payload["label"] = label
    row = admin.create_poi(conn, event_id, payload)
    pois[name] = row["id"]
    return row["id"]
place("Start / finish", "finish", 0, ["Full", "Half", "10K"], "S")
place("1", "aid_station", n // 10, ["Full", "Half", "10K"])
place("2", "aid_station", n // 5, ["Full", "Half", "10K"])
place("3", "aid_station", int(n * 0.32), ["Full", "Half"])
place("4", "aid_station", int(n * 0.45), ["Full", "Half"])
place("5", "aid_station", int(n * 0.62), ["Full"])
place("6", "aid_station", int(n * 0.78), ["Full"])
place("7", "aid_station", int(n * 0.9), ["Full"])
place("A", "water_stops", int(n * 0.15), ["Full", "Half", "10K"])
place("B", "water_stops", int(n * 0.55), ["Full"])
place("Medical tent", "medical", 2, ["Full", "Half", "10K"])
place("Volunteer parking", "parking", 5, [])

roster = [
    ("N0DEM-3", "Aid 1", "Marcus", "aid_station", False, "1"),
    ("N0DEM-4", "Aid 2", "Rae", "aid_station", False, "2"),
    ("N0DEM-5", "Aid 3", "Ollie", "aid_station", False, "3"),
    ("N0DEM-6", "Aid 4", "Sam", "aid_station", False, "4"),
    ("N0DEM-7", "Aid 5", "Jules", "aid_station", False, "5"),
    ("N0DEM-8", "Aid 6", "Kit", "aid_station", False, "6"),
    ("N0DEM-9", "Aid 7", "Avery", "aid_station", False, "7"),
    ("W0RRC-1", "Net Control", "Dana", "net_control", False, None),
    ("AC0DEM-9", "Course rover", "Wes", "rover", True, None),
    ("WB0DEM-9", "SAG 1", "Hollis", "sag", True, None),
    ("WB0DEM-7", "SAG 2", "Nia", "sag", True, None),
    ("KC0DEM-2", "Start/finish", "Priya", "start_finish", False, "Start / finish"),
    ("KD0DEM-9", "Sweep - Full", "Tomas", "sweep", True, None),
    ("KD0DEM-8", "Sweep - Half", "Bea", "sweep", True, None),
]
for key, label, who, cat, aprs, post in roster:
    db.upsert_roster_entry(conn, event_id, key, label, cat, expects_aprs=aprs, operator_name=who)
    if post:
        db.assign_station_to_poi(conn, event_id, key, pois[post])
for key in ("N0DEM-3", "N0DEM-4", "N0DEM-5", "N0DEM-6", "N0DEM-7", "KC0DEM-2", "W0RRC-1",
            "AC0DEM-9", "WB0DEM-9", "WB0DEM-7", "KD0DEM-9", "KD0DEM-8"):
    db.set_op_status(conn, event_id, key, "active", changed_by="W0RRC")
db.set_op_status(conn, event_id, "N0DEM-8", "pending", changed_by="W0RRC")

now = datetime.now(timezone.utc)
def pos(key, i, minutes_ago, speed=0.0, symbol=("/", ">")):
    lat, lon = at(i)
    t = (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.insert_position(conn, event_id, PositionReport(
        station_key=key, received_at=t, lat=lat, lon=lon, course_deg=90.0,
        speed_kmh=speed, altitude_m=None, symbol_table=symbol[0], symbol_code=symbol[1],
        comment="", aprs_format="uncompressed", raw=""))
pos("AC0DEM-9", int(n * 0.40), 2, 38.0)
pos("WB0DEM-9", int(n * 0.58), 4, 24.0)
pos("WB0DEM-7", int(n * 0.22), 13, 0.0)          # stale
pos("KD0DEM-9", int(n * 0.30), 3, 9.0, ("/", "["))
pos("KD0DEM-8", int(n * 0.12), 26, 0.0, ("/", "["))  # silent
pos("KD0DEM-5", int(n * 0.70), 3, 41.0)  # rostered base, SSID the roster does not name

# Pickups and a course note.
def inc(bib, note, i, status, minutes_ago, kind="pickup"):
    lat, lon = at(i)
    row = incidents.create(conn, event_id, lat, lon, bib=bib, note=note, by="W0RRC", kind=kind)
    if status != "reported":
        incidents.set_status(conn, event_id, row["id"], status, by="W0RRC")
    t = (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE incident SET status_at = ?, reported_at = ? WHERE id = ?", (t, t, row["id"]))
inc("1487", "Runner walking, wants a ride to the finish.", int(n * 0.52), "reported", 6)
inc(None, "Called in by a spectator; bib unknown.", int(n * 0.66), "reported", 3)
inc("322", "Cramping at the turn, sitting on the curb.", int(n * 0.20), "en_route", 11)
inc("908", "In the vehicle, heading to the finish.", int(n * 0.13), "picked_up", 18)
inc("2051", "", int(n * 0.35), "dropped_off", 40)
inc(None, "Cone down at the CR 57 crossing, traffic getting through.", int(n * 0.44), "reported", 9, kind="note")
conn.commit()

# Lead runners.
def sight(course, division, place, bib, minutes_ago):
    row = leaders.record_sighting(conn, event_id, course_ids[course], division, pois[place], bib=bib, by="W0RRC")
    t = (now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE lead_sighting SET at = ? WHERE id = ?", (t, row["id"]))
sight("Full", "male", "3", "17", 51)
sight("Full", "male", "4", "17", 23)
sight("Full", "female", "3", "24", 46)
sight("Full", "female", "4", "24", 16)
sight("Half", "male", "1", "301", 44)
sight("Half", "male", "2", "301", 28)
sight("Half", "female", "2", "288", 19)
conn.commit()

tokens = access.ensure_tokens(conn, event_id)
conn.commit()
print(f"seeded {DB}: event riverbend2026, sign in as demo / demo-demo-demo")
for role, tok in sorted(tokens.items()):
    print(f"  {role:10} /e/riverbend2026/{tok}")
