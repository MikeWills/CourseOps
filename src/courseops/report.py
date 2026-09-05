"""The after-event page for the race lead (GitHub issue #7).

One page, printable, screenshot-able. What the organizer wants to know after
the race is small and specific: how many pickups there were and over what
window, which water stops they clustered at, and what went wrong where - a
blocked intersection, a confusing turn, a marshal who never arrived. That is
the whole page. It carries no names: not the volunteers who reported things,
not who moved a pickup along, and certainly never a runner's name - we hold
bibs and nothing that maps a bib to a person.

Times are rendered in the browser, in the event's time zone. Every timestamp
here is stored UTC, and this is the first place the stored `event.timezone`
is actually read: "07:42" has to be the event's 07:42, not the reader's, and
the browser has the zone database that a Windows install of Python does not.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from html import escape

from . import categories, geo, incidents, progress

# The same Leaflet and the same tiles the live map uses, so the small maps on
# this page cost nothing new: no dependency, no server-side call, nothing sent
# anywhere the live map does not already send it. The browser draws them, which
# is also what makes them survive printing and screenshots.
LEAFLET_CSS = ("https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
               "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=")
LEAFLET_JS = ("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
              "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=")
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
MINI_MAP_ZOOM = 16

# A pickup counts against a water stop if it was called in within this
# distance of one. Further than that and it happened between stops, which is
# its own answer - a run of pickups on a stretch with no stop is something the
# organizer can act on.
NEAR_STOP_M = 800.0


@dataclass
class Place:
    name: str
    count: int = 0


@dataclass
class Note:
    at: str                  # ISO-8601 UTC, as stored
    where: str               # "near Aid 4" / "mile 9.1 of Half" / lat, lon
    text: str
    lat: float
    lon: float


@dataclass
class Report:
    event_name: str
    event_date: str | None
    timezone: str
    pickups: int
    first_pickup: str | None
    last_pickup: str | None
    by_place: list[Place] = field(default_factory=list)
    between_stops: int = 0
    notes: list[Note] = field(default_factory=list)
    # Drawn under each note's map so the corner is seen in relation to the
    # route, which is what makes "mile 9.1" recognisable as a place.
    courses: list[dict] = field(default_factory=list)


def _mile(index: progress.CourseIndex, lat: float, lon: float) -> str | None:
    located = index.locate(lat, lon)
    if not located:
        return None
    return f"mile {located.distance_along_m / 1609.344:.1f} of {located.course_name}"


def build(conn: sqlite3.Connection, event_id: int) -> Report:
    event = conn.execute("SELECT * FROM event WHERE id = ?", (event_id,)).fetchone()
    if event is None:
        raise KeyError(event_id)

    index = progress.CourseIndex.for_event(conn, event_id)
    staffed = categories.staffed_keys(conn, event_id)
    stops = [
        row for row in index.order_along_course(
            conn.execute("SELECT * FROM poi WHERE event_id = ?", (event_id,)).fetchall()
        )
        if row["poi_type"] in staffed
    ]
    places = {row["id"]: Place(name=row["name"]) for row in stops}

    def nearest_stop(lat: float, lon: float):
        best, best_m = None, NEAR_STOP_M
        for row in stops:
            d = geo.haversine_m((lon, lat), (row["lon"], row["lat"]))
            if d < best_m:
                best, best_m = row, d
        return best

    report = Report(
        event_name=event["name"], event_date=event["event_date"],
        timezone=event["timezone"] or "UTC",
        pickups=0, first_pickup=None, last_pickup=None,
    )

    rows = sorted(incidents.for_event(conn, event_id),
                  key=lambda r: r["reported_at"])
    for row in rows:
        kind = row["kind"] or incidents.KIND_PICKUP
        if kind == incidents.KIND_PICKUP:
            report.pickups += 1
            report.first_pickup = report.first_pickup or row["reported_at"]
            report.last_pickup = row["reported_at"]
            stop = nearest_stop(row["lat"], row["lon"])
            if stop is None:
                report.between_stops += 1
            else:
                places[stop["id"]].count += 1
        else:
            stop = nearest_stop(row["lat"], row["lon"])
            mile = _mile(index, row["lat"], row["lon"])
            where = ", ".join(
                part for part in (f"near {stop['name']}" if stop else None, mile)
                if part
            ) or f"{row['lat']:.5f}, {row['lon']:.5f}"
            report.notes.append(Note(
                at=row["reported_at"], where=where,
                text=row["note"] or "(no details recorded)",
                lat=row["lat"], lon=row["lon"],
            ))

    report.by_place = [p for p in places.values() if p.count]
    if report.notes:
        for row in conn.execute(
            "SELECT name, color, geojson FROM course WHERE event_id = ?"
            " ORDER BY sort_order, id", (event_id,)
        ):
            try:
                coords = json.loads(row["geojson"])["coordinates"]
            except (ValueError, KeyError, TypeError):
                continue
            report.courses.append({
                "name": row["name"], "color": row["color"] or "#cc3333",
                "coordinates": coords,
            })
    return report


def _time(iso: str | None) -> str:
    if not iso:
        return "-"
    return f'<time datetime="{escape(iso)}">{escape(iso)}</time>'


def render(report: Report) -> str:
    """The page. Self-contained: inline style, one tiny script for the clock."""
    e = escape
    if report.pickups == 0:
        pickups = "<p class=\"big\">No pickups.</p>"
    else:
        noun = "pickup" if report.pickups == 1 else "pickups"
        window = (
            f" between {_time(report.first_pickup)} and {_time(report.last_pickup)}"
            if report.first_pickup != report.last_pickup
            else f" at {_time(report.first_pickup)}"
        )
        pickups = f"<p class=\"big\">{report.pickups} {noun}{window}.</p>"
        rows = "".join(
            f"<tr><td>{e(p.name)}</td><td class=\"n\">{p.count}</td></tr>"
            for p in report.by_place
        )
        if report.between_stops:
            rows += (f"<tr><td>Between stops</td>"
                     f"<td class=\"n\">{report.between_stops}</td></tr>")
        if rows:
            pickups += (
                "<table><thead><tr><th>Near</th><th class=\"n\">Pickups</th>"
                f"</tr></thead><tbody>{rows}</tbody></table>"
            )

    if report.notes:
        # One card per note: a small map of the corner beside the words. A
        # table row cannot carry a map, and a printed list of "mile 9.1"s is
        # far less use to a route committee than seeing each one.
        notes = "".join(
            f'<article class="note">'
            f'<div class="mini" data-lat="{n.lat:.6f}" data-lon="{n.lon:.6f}"></div>'
            f'<div class="words"><p class="when">{_time(n.at)}'
            f' &middot; {e(n.where)}</p><p class="what">{e(n.text)}</p></div>'
            f'</article>'
            for n in report.notes
        )
    else:
        notes = "<p class=\"big\">Nothing to report.</p>"

    # Course lines for the small maps. JSON inside a <script> is the one
    # place HTML escaping does not apply, so "<" is written as its JSON
    # escape - a course NAME is club-typed text and must not be able to end
    # the block.
    courses_json = json.dumps(report.courses).replace("<", "\\u003c")

    date = f" &middot; {e(report.event_date)}" if report.event_date else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(report.event_name)} - report</title>
<link rel="stylesheet" href="{LEAFLET_CSS[0]}" integrity="{LEAFLET_CSS[1]}" crossorigin="">
<style>
  body {{ margin: 0; padding: 20px 24px 32px; max-width: 720px;
         font: 16px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         color: #14181d; background: #fff; }}
  header {{ border-bottom: 3px solid #ff7a1a; padding-bottom: 10px; margin-bottom: 20px; }}
  h1 {{ margin: 0; font-size: 22px; }}
  h2 {{ font-size: 15px; text-transform: uppercase; letter-spacing: .06em;
        color: #4a5561; margin: 26px 0 8px; }}
  .sub {{ color: #4a5561; margin: 4px 0 0; }}
  .big {{ font-size: 19px; font-weight: 650; margin: 6px 0 12px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 7px 8px; border-bottom: 1px solid #e3e8ee;
            vertical-align: top; }}
  th {{ font-size: 13px; color: #4a5561; font-weight: 600; }}
  td.n, th.n {{ text-align: right; font-variant-numeric: tabular-nums; }}
  time {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
  footer {{ margin-top: 30px; font-size: 13px; color: #4a5561; }}
  .note {{ display: flex; gap: 14px; padding: 12px 0; border-bottom: 1px solid #e3e8ee;
           break-inside: avoid; }}
  .mini {{ flex: 0 0 220px; height: 160px; border: 1px solid #c7d2e0; border-radius: 6px;
           background: #eef2f6; }}
  .words {{ flex: 1; min-width: 0; }}
  .when {{ margin: 0 0 4px; color: #4a5561; font-size: 14px; }}
  .what {{ margin: 0; font-size: 17px; font-weight: 600; }}
  /* The note's own marker: a plain dot, so it cannot be mistaken for a stop. */
  .dot {{ width: 14px; height: 14px; border-radius: 50%; background: #c8321e;
          border: 3px solid #fff; box-shadow: 0 0 0 1px #c8321e; }}
  @media (max-width: 520px) {{ .note {{ flex-direction: column; }}
                               .mini {{ flex-basis: auto; width: 100%; }} }}
  @media print {{ body {{ padding: 0; }} footer {{ display: none; }}
                  .mini {{ border-color: #999; }} }}
</style></head>
<body>
<header>
  <h1>{e(report.event_name)}</h1>
  <p class="sub">Course report{date} &middot; times are <span id="tz">{e(report.timezone)}</span></p>
</header>

<h2>Pickups</h2>
{pickups}

<h2>Course notes</h2>
{notes}

<footer>Counts and locations only. No names are recorded here.</footer>

<script>
/* Stored UTC; shown in the event's zone. The browser has the zone database;
   a Windows install of Python may not. If the zone name is unknown, fall
   back to the reader's own zone and say so, rather than showing UTC without
   a label - a time with the wrong zone on it is worse than none. */
(function () {{
  /* Read from the escaped element, never interpolated into this script: a
     zone name is admin-typed text, and text inside a <script> is the one
     place HTML escaping does not protect. */
  var tz = document.getElementById("tz").textContent;
  var opts = {{ hour: "2-digit", minute: "2-digit", hour12: false }};
  var fmt;
  try {{ fmt = new Intl.DateTimeFormat(undefined, Object.assign({{ timeZone: tz }}, opts)); }}
  catch (err) {{
    fmt = new Intl.DateTimeFormat(undefined, opts);
    document.getElementById("tz").textContent = "your local zone (event zone " + tz + " unknown)";
  }}
  document.querySelectorAll("time[datetime]").forEach(function (el) {{
    var d = new Date(el.getAttribute("datetime"));
    if (!isNaN(d)) el.textContent = fmt.format(d);
  }});
}})();
</script>
<script id="courses" type="application/json">{courses_json}</script>
<script src="{LEAFLET_JS[0]}" integrity="{LEAFLET_JS[1]}" crossorigin=""></script>
<script>
/* A small, still map for each note: the course line for context and a dot
   where it happened. Not interactive - this page is printed or screenshotted,
   and a map that pans under a thumb is a map that shows the wrong corner.
   If Leaflet did not load (no network), the box stays a plain grey square
   and the words beside it still say where. */
(function () {{
  if (typeof L === "undefined") return;
  var courses = JSON.parse(document.getElementById("courses").textContent || "[]");
  document.querySelectorAll(".mini[data-lat]").forEach(function (el) {{
    var lat = Number(el.dataset.lat), lon = Number(el.dataset.lon);
    var map = L.map(el, {{ zoomControl: false, dragging: false, scrollWheelZoom: false,
      doubleClickZoom: false, touchZoom: false, boxZoom: false, keyboard: false,
      attributionControl: false }}).setView([lat, lon], {MINI_MAP_ZOOM});
    L.tileLayer({TILE_URL!r}, {{ maxZoom: 19 }}).addTo(map);
    courses.forEach(function (c) {{
      L.polyline(c.coordinates.map(function (p) {{ return [p[1], p[0]]; }}),
        {{ color: c.color, weight: 4, opacity: 0.85 }}).addTo(map);
    }});
    L.marker([lat, lon], {{ icon: L.divIcon({{ className: "", iconSize: [14, 14],
      iconAnchor: [7, 7], html: '<div class="dot"></div>' }}), interactive: false }}).addTo(map);
  }});
}})();
</script>
</body></html>
"""
