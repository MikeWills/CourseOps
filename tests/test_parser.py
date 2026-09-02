"""Parser regression tests.

The point of a fixture corpus is that parser changes can be checked without a
live network connection, and that a packet which once broke us stays broken-proof.
When live ingest hits a parse error worth caring about, add the line here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aprswebtracker.parser import Rejected, parse_packet

FIXTURES = Path(__file__).parent / "fixtures" / "packets.txt"


def load_cases() -> list[tuple[str, str]]:
    cases = []
    for line in FIXTURES.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        expectation, _, raw = line.partition("|")
        cases.append((expectation.strip(), raw))
    return cases


@pytest.mark.parametrize("expectation,raw", load_cases())
def test_fixture_corpus(expectation: str, raw: str) -> None:
    if expectation in {"no_position", "parse_error"}:
        with pytest.raises(Rejected) as excinfo:
            parse_packet(raw)
        assert excinfo.value.reason == expectation
    else:
        report = parse_packet(raw)
        assert report.station_key == expectation
        assert -90.0 <= report.lat <= 90.0
        assert -180.0 <= report.lon <= 180.0


def test_ssid_is_part_of_identity():
    """N0CALL-9 and N0CALL-7 are different radios on different people."""
    base = "{}>APDR16,TCPIP*,qAC,T2USA:!3444.00N/08635.00W>"
    assert parse_packet(base.format("N0CALL-9")).station_key == "N0CALL-9"
    assert parse_packet(base.format("N0CALL-7")).station_key == "N0CALL-7"


def test_symbol_table_and_code_stay_paired():
    report = parse_packet(
        "KI4HMD-1>APX204,TCPIP*,qAC,FOURTH:=3450.29N/08639.24W-House"
    )
    assert report.symbol_table == "/"
    assert report.symbol_code == "-"


def test_null_island_is_rejected():
    """A tracker with no GPS fix reports 0,0. That is not a position."""
    with pytest.raises(Rejected) as excinfo:
        parse_packet("N0CALL-9>APRS,TCPIP*,qAC,X:!0000.00N/00000.00W>No fix")
    assert excinfo.value.reason == "no_position"


def test_mic_e_yields_course_and_speed():
    """Mic-E hides latitude in the AX.25 destination field; verify it decodes."""
    report = parse_packet("KJ4ERJ-12>APWW11,TCPIP*,qAC,T2SYDNEY:`b7ml!_j/'\"4S}")
    assert report.aprs_format == "mic-e"
    assert report.course_deg is not None
    assert report.speed_kmh is not None


def test_unexpected_beacon_from_no_aprs_operator_is_stored(tmp_path):
    """An aid station operator who turns a tracker on is still one of ours."""
    from aprswebtracker import db
    from aprswebtracker.ingest import IngestStats, handle_line

    conn = db.connect(tmp_path / "t.sqlite3")
    db.init_schema(conn)
    event_id = db.create_event(conn, "e", "Event")
    db.upsert_roster_entry(
        conn, event_id, "KI4HMD-1", "Aid 4", "aid_station", expects_aprs=False
    )

    roster_keys = set(db.all_station_keys(conn, event_id))
    stats = IngestStats()
    report = handle_line(
        conn, event_id, roster_keys,
        "KI4HMD-1>APX204,TCPIP*,qAC,FOURTH:=3450.29N/08639.24W-Aid 4",
        stats,
    )
    assert report is not None
    assert stats.stored == 1
    assert stats.not_rostered == 0
