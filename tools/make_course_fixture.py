"""Generate the synthetic 'consumer export' course fixture.

    python tools/make_course_fixture.py

This replaces a real organizer's export that used to live here. That file was
genuinely useful - it is how several defects in this project were found - but it
was somebody else's data, and it is not ours to publish. So the *findings* are
kept and the file is not: this reproduces the same defect profile, deliberately,
so the tests still guard the same behaviours.

What it recreates, and why each one earns its place:

  * **A point-to-point route**, not a loop. Real courses start and finish in
    different places, and code that assumes otherwise looks correct on a loop.
  * **Consecutive duplicate points** where the exporter joined segments. These
    have to be deduped or every distance is slightly wrong.
  * **Straight-line gaps** where the route builder used direct/offroad mode.
    Chords are shorter than the road, so mile figures drift on such files. The
    app must not silently smooth them - it reports them.
  * **A start and a finish sharing the route's name**, distinguishable only by
    <styleUrl>. Consumer exporters really do this, and without the style they
    are indistinguishable in the review list. The style ids use underscores
    (`start_marker`) on purpose: `_` is a word character, so a -anchored hint
    pattern will not match inside one unless it normalises separators first.
    That was a real bug, and this is what keeps it fixed.
  * **No folders, and no aid stations.** Neither is present in consumer exports,
    so nothing may depend on them being there.

The numbers below are chosen to match what the real file had, so the assertions
in tests/test_real_course.py continue to mean what they meant.
"""

from __future__ import annotations

import math
import pathlib

OUT = pathlib.Path(__file__).resolve().parent.parent / "tests" / "fixtures"
FILENAME = "consumer_export_course.kml"

# Mankato, Minnesota - the same part of the world the project was written
# against, so map screenshots and manual checks still look plausible.
START = (-94.0000, 44.1600)

TARGET_MILES = 26.30
POINTS = 1415
DUPLICATES = 157          # consecutive repeats an exporter leaves at joins
GAP_COUNT = 13            # straight-line jumps over 200 m
LONGEST_GAP_M = 1241.0


def destination(lon: float, lat: float, bearing_deg: float, metres: float):
    """Move a point along a bearing. Flat-earth is fine over a marathon."""
    lat_m = 111_320.0
    lon_m = 111_320.0 * math.cos(math.radians(lat))
    rad = math.radians(bearing_deg)
    return (lon + (metres * math.sin(rad)) / lon_m,
            lat + (metres * math.cos(rad)) / lat_m)


def build_route() -> list[tuple[float, float]]:
    total_m = TARGET_MILES * 1609.344
    # The gaps carry distance too, so the ordinary steps make up the rest.
    gap_lengths = [LONGEST_GAP_M * (0.35 + 0.65 * i / (GAP_COUNT - 1))
                   for i in range(GAP_COUNT)]
    gap_lengths[-1] = LONGEST_GAP_M
    walked = total_m - sum(gap_lengths)
    steps = POINTS - DUPLICATES - GAP_COUNT - 1
    step_m = walked / steps

    # Where the gaps go: spread through the middle, never at either end, so the
    # first and last points stay the true start and finish.
    gap_at = {int(steps * (i + 1) / (GAP_COUNT + 1)) for i in range(GAP_COUNT)}
    dup_at = {int(steps * (i + 1) / (DUPLICATES + 1)) for i in range(DUPLICATES)}

    lon, lat = START
    coords = [(lon, lat)]
    bearing = 20.0
    gaps_used = 0

    for i in range(steps):
        # A meandering course rather than a straight line, so bounds, centroids
        # and course-relative position have something realistic to chew on.
        bearing += 26.0 * math.sin(i / 37.0) + 9.0 * math.sin(i / 7.3)
        lon, lat = destination(lon, lat, bearing, step_m)
        coords.append((lon, lat))

        if i in gap_at and gaps_used < GAP_COUNT:
            lon, lat = destination(lon, lat, bearing, gap_lengths[gaps_used])
            coords.append((lon, lat))
            gaps_used += 1

        if i in dup_at:
            coords.append((lon, lat))       # the exporter's join artefact

    return coords


def kml(coords: list[tuple[float, float]]) -> str:
    line = " ".join(f"{lon:.6f},{lat:.6f},0" for lon, lat in coords)
    start_lon, start_lat = coords[0]
    end_lon, end_lat = coords[-1]

    # Note what is NOT here: no <Folder>, no aid stations, and the two points
    # share the route's name. All three are true of real consumer exports.
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>Example Marathon</name>
    <Style id="route_line">
      <LineStyle><color>ff0000ff</color><width>4</width></LineStyle>
    </Style>
    <Style id="start_marker">
      <IconStyle><Icon><href>start.png</href></Icon></IconStyle>
    </Style>
    <Style id="finish_marker">
      <IconStyle><Icon><href>finish.png</href></Icon></IconStyle>
    </Style>
    <Placemark>
      <name>Example Marathon</name>
      <styleUrl>#route_line</styleUrl>
      <LineString><tessellate>1</tessellate>
        <coordinates>{line}</coordinates>
      </LineString>
    </Placemark>
    <Placemark>
      <name>Example Marathon</name>
      <styleUrl>#start_marker</styleUrl>
      <Point><coordinates>{start_lon:.6f},{start_lat:.6f},0</coordinates></Point>
    </Placemark>
    <Placemark>
      <name>Example Marathon</name>
      <styleUrl>#finish_marker</styleUrl>
      <Point><coordinates>{end_lon:.6f},{end_lat:.6f},0</coordinates></Point>
    </Placemark>
  </Document>
</kml>
"""


def main() -> None:
    coords = build_route()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / FILENAME
    path.write_text(kml(coords), encoding="utf-8")

    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
    from courseops import geo

    gaps = [geo.haversine_m(coords[i], coords[i + 1])
            for i in range(len(coords) - 1)]
    print(f"wrote {path}")
    print(f"  points        {len(coords)}")
    print(f"  deduped       {len(geo.dedupe_consecutive(coords))}")
    print(f"  distance      {geo.line_length_m(coords) / 1609.344:.2f} mi")
    print(f"  gaps > 200 m  {len([g for g in gaps if g > 200])}")
    print(f"  longest gap   {max(gaps):,.0f} m")
    print(f"  start->finish {geo.haversine_m(coords[0], coords[-1]):,.0f} m")


if __name__ == "__main__":
    main()
