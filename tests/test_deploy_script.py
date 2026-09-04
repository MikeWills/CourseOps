"""The deploy script, checked for the traps that only show up in production.

Nothing here runs the script - it touches a real server. These assert the
shape of it, because the failures they guard against are silent: a deploy that
reports success and changes nothing is worse than one that fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "deploy.sh"
WORKFLOW = (Path(__file__).resolve().parents[1]
            / ".github" / "workflows" / "deploy.yml")


@pytest.fixture(scope="module")
def script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_ref_is_resolved_before_it_is_checked_out(script):
    """`git checkout main` on the server does NOT deploy origin/main.

    The working copy sits on a detached HEAD, so a local `main` is whatever it
    was when the server was cloned. Checking it out deploys stale code, reports
    success, and leaves someone staring at a version that did not change.
    """
    assert 'refs/remotes/origin/$TAG' in script
    assert "--detach" in script
    # The bare form is the bug. It must not come back.
    assert not re.search(r'checkout --force "\$TAG"', script)


def test_an_unknown_ref_stops_rather_than_deploying_something(script):
    """A typo must not silently deploy whatever HEAD happens to be."""
    assert "No tag or branch called" in script
    assert re.search(r"exit 1", script)


def test_a_tag_wins_over_a_branch_of_the_same_name(script):
    tag_at = script.index("refs/tags/$TAG")
    branch_at = script.index("refs/remotes/origin/$TAG")
    assert tag_at < branch_at, "tags must be tried first"


def test_the_workflow_can_be_run_by_hand_against_a_branch():
    """The escape hatch for being away from a terminal: Actions -> Run
    workflow, which should not require inventing a tag from a phone."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "default: main" in text


def test_the_workflow_does_not_claim_to_wait_for_something_it_does_not():
    """The header used to say it waited for the release build's artefacts. It
    never did, and the server installs from git, so nothing was waiting."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "waits for the release build" not in text
