"""What `courseops serve` prints, and where.

The role links are the credentials. Under systemd stdout is the journal, and
a journal line is kept, rotated and readable by a group - so the links are
printed to a terminal and nowhere else.
"""

from __future__ import annotations

import io
import sys
import types

import pytest

from courseops import access, cli, db


class _Terminal(io.StringIO):
    def isatty(self):
        return True


def test_links_go_to_a_terminal_and_not_to_a_log():
    assert cli.links_are_printable(_Terminal()) is True
    assert cli.links_are_printable(io.StringIO()) is False


def test_a_closed_or_odd_stream_counts_as_a_log():
    class Broken:
        def isatty(self):
            raise ValueError("closed")
    assert cli.links_are_printable(Broken()) is False
    assert cli.links_are_printable(object()) is False


@pytest.fixture
def served(tmp_path, monkeypatch):
    """Run cmd_serve with uvicorn stubbed out, returning what it printed."""
    db_path = tmp_path / "t.sqlite3"
    conn = db.connect(db_path)
    db.init_schema(conn)
    event_id = db.create_event(conn, "m2026", "Spring Marathon")
    tokens = access.ensure_tokens(conn, event_id)
    conn.close()
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("APRS_CALLSIGN", "")
    monkeypatch.setitem(sys.modules, "uvicorn",
                        types.SimpleNamespace(run=lambda *a, **k: None))

    def run(stream):
        monkeypatch.setattr(sys, "stdout", stream)
        args = cli.build_parser().parse_args(["serve", "m2026", "--no-ingest"])
        args.func(args)
        return stream.getvalue(), tokens

    return run


def test_serve_prints_the_links_on_a_terminal(served):
    out, tokens = served(_Terminal())
    for token in tokens.values():
        assert token in out


def test_serve_keeps_the_links_out_of_the_journal(served):
    """Under systemd stdout is the journal: every restart and every deploy
    would write all five credentials there, and revoking a link does
    nothing about old journal lines."""
    out, tokens = served(io.StringIO())
    for token in tokens.values():
        assert token not in out
    # And says where they are instead, rather than printing nothing.
    assert "Links tab" in out
