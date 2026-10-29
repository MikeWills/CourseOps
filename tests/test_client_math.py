"""`versionKey` and the two `metresBetween` copies, run in Node.

`versionKey` decides whether the setup screen tells an officer a new build has
landed; if it ever returned "" for a signed-in admin the notice would be off
for good, silently (CLAUDE.md: "It compares the BUILD, not the version").
`metresBetween` orders the pickup queue by distance away, and setup.js carries
its own copy with a different signature and a different formula, so a test
holds the two to the same answer where they are used. Same pattern as
test_client_time.py: cut the function out of the file, run it in Node, skip
when Node is missing.
"""

import json
import shutil
import subprocess

import pytest

from courseops import geo, web

APP_JS = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
SETUP_JS = (web.STATIC_DIR / "setup.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    return source[start:source.index("\n}\n", start) + 3]


def _run(tmp_path, functions: str, expression: str):
    script = functions + f"\nconsole.log(JSON.stringify({expression}));"
    path = tmp_path / "t.js"
    path.write_text(script, encoding="utf-8")
    out = subprocess.run([NODE, str(path)], capture_output=True, check=True,
                         timeout=30).stdout.decode("utf-8")
    return json.loads(out)


@pytest.mark.parametrize("info, expected", [
    # The build wins: deploys of a branch all carry one version.
    ({"build": "abc1234", "version": "2026.10.19"}, "abc1234"),
    # No git behind the build: the version has to do.
    ({"build": "", "version": "2026.10.19"}, "2026.10.19"),
    ({"version": "2026.10.19"}, "2026.10.19"),
    # Signed out, the session says nothing about either.
    ({}, ""),
    (None, ""),
])
def test_version_key_prefers_the_build(tmp_path, info, expected):
    source = _function(SETUP_JS, "versionKey")
    assert _run(tmp_path, source, f"versionKey({json.dumps(info)})") == expected


def test_version_key_differs_between_builds_of_one_version(tmp_path):
    source = _function(SETUP_JS, "versionKey")
    out = _run(tmp_path, source, (
        'versionKey({build: "aaa", version: "1"}) !== '
        'versionKey({build: "bbb", version: "1"})'))
    assert out is True


def _app_metres(tmp_path, a, b):
    source = _function(APP_JS, "metresBetween")
    return _run(tmp_path, source,
                f"metresBetween({json.dumps(a)}, {json.dumps(b)})")


def _setup_metres(tmp_path, a, b):
    source = _function(SETUP_JS, "metresBetween")
    return _run(tmp_path, source,
                f"metresBetween({a['lat']}, {a['lon']}, {b['lat']}, {b['lon']})")


MANKATO = {"lat": 44.1636, "lon": -94.0000}


def test_metres_between_the_same_point_is_zero(tmp_path):
    assert _app_metres(tmp_path, MANKATO, MANKATO) == 0
    assert _setup_metres(tmp_path, MANKATO, MANKATO) == 0


def test_metres_between_is_symmetric(tmp_path):
    other = {"lat": 44.1700, "lon": -93.9800}
    assert _app_metres(tmp_path, MANKATO, other) == pytest.approx(
        _app_metres(tmp_path, other, MANKATO))


def test_one_degree_of_latitude_is_about_111_km(tmp_path):
    north = {"lat": MANKATO["lat"] + 1, "lon": MANKATO["lon"]}
    assert _app_metres(tmp_path, MANKATO, north) == pytest.approx(111_195, abs=5)


def test_the_browser_agrees_with_the_servers_haversine(tmp_path):
    """The queue's "away" figure must match what geo.py would say."""
    other = {"lat": 44.1802, "lon": -93.9511}
    expected = geo.haversine_m((MANKATO["lon"], MANKATO["lat"]),
                               (other["lon"], other["lat"]))
    # geo.py uses the IUGG mean radius (6,371,008.8 m) and the browser a flat
    # 6,371,000 m: about 6 mm over 4 km. A wrong formula would be off by metres.
    assert _app_metres(tmp_path, MANKATO, other) == pytest.approx(expected, rel=1e-5)


def test_longitude_degrees_shrink_with_latitude(tmp_path):
    # A degree of longitude is shorter at Mankato than at the equator; swapping
    # lat and lon anywhere in the call would not notice at (0, 0).
    east = {"lat": MANKATO["lat"], "lon": MANKATO["lon"] + 1}
    metres = _app_metres(tmp_path, MANKATO, east)
    assert 79_000 < metres < 81_000


def test_the_setup_copy_agrees_with_the_browser_copy_over_a_course(tmp_path):
    """setup.js uses a flat-earth shortcut; over a course it must stay close.

    The picker snaps and compares places a few kilometres apart, so half a
    percent is the most it may drift from the haversine the live map uses.
    """
    other = {"lat": 44.1802, "lon": -93.9511}   # about 4 km away
    flat = _setup_metres(tmp_path, MANKATO, other)
    true = _app_metres(tmp_path, MANKATO, other)
    assert flat == pytest.approx(true, rel=0.005)
