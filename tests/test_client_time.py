"""The time and pace helpers in app.js, run in Node.

`clockTime`, `ageSeconds`, `formatAge` and `paceLabel` are the figures NCS reads
aloud and the "silent 18 min" the whole radio-status axis hangs on, and nothing
exercised them: the tests that mention app.js read its source as text. They are
pure apart from `state.event.timezone` and `Date.now`, so each is cut out of
the file and run with those two stubbed. Same pattern as test_station_order.py:
no JS test runner, skipped when Node is missing.
"""

import json
import shutil
import subprocess

import pytest

from courseops import web

APP_JS = (web.STATIC_DIR / "app.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _function(name: str) -> str:
    start = APP_JS.index(f"function {name}(")
    return APP_JS[start:APP_JS.index("\n}\n", start) + 3]


def _clock_source() -> str:
    # The memoised formatter lives beside the function and is part of its behaviour.
    start = APP_JS.index("let clockFormatter = null;")
    end = APP_JS.index("\n}\n", APP_JS.index("function clockTime(")) + 3
    return APP_JS[start:end]


def _run(tmp_path, body: str, *, zone: str = "", now: str = "2026-10-17T12:00:00Z"):
    script = (
        f"const state = {{ event: {{ timezone: {json.dumps(zone)} }} }};\n"
        f"Date.now = () => Date.parse({json.dumps(now)});\n"
        + _clock_source() + "\n"
        + _function("ageSeconds") + "\n"
        + _function("formatAge") + "\n"
        + _function("paceLabel") + "\n"
        + body
    )
    # A file, not `-e`: a non-ASCII argument is mangled on a Windows command line.
    path = tmp_path / "t.js"
    path.write_text(script, encoding="utf-8")
    out = subprocess.run([NODE, str(path)], capture_output=True, check=True,
                         timeout=30).stdout.decode("utf-8")
    return json.loads(out)


def _call(tmp_path, expression: str, **kwargs):
    return _run(tmp_path, f"console.log(JSON.stringify({expression}));", **kwargs)


@pytest.mark.parametrize("stored, zone, expected", [
    # Stored UTC, shown in the EVENT's zone: October in Minnesota is UTC-5.
    ("2026-10-17T19:20:00Z", "America/Chicago", "14:20"),
    # The same instant in another zone is a different wall clock - which is
    # why the zone is the event's and never the phone's.
    ("2026-10-17T19:20:00Z", "America/New_York", "15:20"),
    # SQLite's shape has no trailing Z; it is UTC all the same.
    ("2026-10-17 19:20:00".replace(" ", "T"), "America/Chicago", "14:20"),
    # Afternoon is 24-hour: "14:20" cannot be heard as the wrong half of the day.
    ("2026-10-17T21:05:00Z", "America/Chicago", "16:05"),
])
def test_clock_time_is_24_hour_in_the_events_zone(tmp_path, stored, zone, expected):
    assert _call(tmp_path, f"clockTime({json.dumps(stored)})", zone=zone) == expected


def test_clock_time_across_midnight_reads_00_not_24(tmp_path):
    # hour12:false in some locales renders midnight as "24:05".
    out = _call(tmp_path, 'clockTime("2026-10-18T05:05:00Z")', zone="America/Chicago")
    assert out == "00:05"


@pytest.mark.parametrize("bad", ["", "not a time"])
def test_clock_time_shows_nothing_for_a_missing_or_unreadable_stamp(tmp_path, bad):
    assert _call(tmp_path, f"clockTime({json.dumps(bad)})", zone="America/Chicago") == ""


def test_clock_time_survives_a_zone_name_intl_does_not_know(tmp_path):
    # Better the viewer's own zone than a blank where a time belongs.
    out = _call(tmp_path, 'clockTime("2026-10-17T19:20:00Z")', zone="Not/AZone")
    assert len(out) == 5 and out[2] == ":"


def test_clock_time_follows_a_change_of_event_zone(tmp_path):
    """The formatter is cached; an admin moving the event must not keep the old one."""
    out = _run(tmp_path, (
        'const iso = "2026-10-17T19:20:00Z";\n'
        'const a = clockTime(iso);\n'
        'state.event.timezone = "America/New_York";\n'
        'console.log(JSON.stringify([a, clockTime(iso)]));'
    ), zone="America/Chicago")
    assert out == ["14:20", "15:20"]


@pytest.mark.parametrize("stored, expected", [
    ("2026-10-17T11:58:00Z", 120),
    ("2026-10-17T11:58:00", 120),            # no Z: still UTC
    ("2026-10-17T12:00:00Z", 0),
    # A clock a little ahead of ours is "just now", never a negative age.
    ("2026-10-17T12:00:30Z", 0),
])
def test_age_seconds(tmp_path, stored, expected):
    assert _call(tmp_path, f"ageSeconds({json.dumps(stored)})") == expected


@pytest.mark.parametrize("bad", ["", "garbage"])
def test_age_seconds_is_null_without_a_usable_stamp(tmp_path, bad):
    # null is what makes radioStatus() say "silent" rather than "fresh".
    assert _call(tmp_path, f"ageSeconds({json.dumps(bad)})") is None


@pytest.mark.parametrize("seconds, expected", [
    (None, "--"),
    (0, "0s"),
    (59.9, "59s"),
    (60, "1m"),
    (18 * 60, "18m"),
    (59 * 60 + 59, "59m"),
    (3600, "1h00"),
    (3600 + 5 * 60, "1h05"),
    (26 * 3600 + 30 * 60, "26h30"),
])
def test_format_age(tmp_path, seconds, expected):
    assert _call(tmp_path, f"formatAge({json.dumps(seconds)})") == expected


@pytest.mark.parametrize("mps, expected", [
    (None, None),
    (0, None),                    # standing still has no pace
    (3.0, "8:56/mi"),             # 1609.344 / 3 / 60 = 8.94 min
    (1609.344 / 600, "10:00/mi"),
    (0.01, None),                 # slower than 60 min a mile is noise, not a pace
])
def test_pace_label(tmp_path, mps, expected):
    assert _call(tmp_path, f"paceLabel({json.dumps(mps)})") == expected


def test_pace_label_never_prints_sixty_seconds(tmp_path):
    # 479.6 s per mile: rounding only the remainder gave "7:60/mi".
    mps = 1609.344 / 479.6
    assert _call(tmp_path, f"paceLabel({mps!r})") == "8:00/mi"
