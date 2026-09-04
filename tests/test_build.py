"""What is running here.

The package version answers "which release is this". The question actually
asked is "did my deploy land?", and deploys of a branch all report the same
version - so the version alone cannot tell a landed deploy from a stale page.
"""

from __future__ import annotations

import subprocess

import pytest

from courseops import build


@pytest.fixture(autouse=True)
def _no_cache():
    """build_id is cached for the life of the process; tests need it fresh."""
    build.build_id.cache_clear()
    yield
    build.build_id.cache_clear()


def test_an_explicit_build_wins(monkeypatch):
    """A frozen build or a container has no git, but may know its commit."""
    monkeypatch.setenv("COURSEOPS_BUILD", "deadbee")
    assert build.build_id() == "deadbee"


def test_a_missing_git_is_not_an_error(monkeypatch):
    """A club's wheel install has no repository. That is not a failure - it
    just means we say the version and nothing we cannot support."""
    monkeypatch.delenv("COURSEOPS_BUILD", raising=False)
    monkeypatch.setattr(build.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError))
    assert build.build_id() == ""
    assert build.version_string() == build.__version__


def test_a_hung_git_does_not_hold_up_the_server(monkeypatch):
    """This runs at import. A slow start is worse than no answer."""
    monkeypatch.delenv("COURSEOPS_BUILD", raising=False)

    def timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=2)

    monkeypatch.setattr(build.subprocess, "run", timeout)
    assert build.build_id() == ""


def test_a_directory_that_is_not_a_checkout_reports_nothing(monkeypatch):
    monkeypatch.delenv("COURSEOPS_BUILD", raising=False)

    class Result:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k: Result())
    assert build.build_id() == ""


def test_the_version_string_carries_the_build_when_there_is_one(monkeypatch):
    monkeypatch.setenv("COURSEOPS_BUILD", "abc1234")
    assert build.version_string() == f"{build.__version__} (abc1234)"


def test_an_absurd_build_string_is_truncated(monkeypatch):
    """It goes in a header. Nothing here should be able to make that unusable."""
    monkeypatch.setenv("COURSEOPS_BUILD", "x" * 500)
    assert len(build.build_id()) <= 40
