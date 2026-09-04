"""What is actually running here.

The package version answers "which release is this" and is useless for the
question that gets asked in practice, which is "did my deploy land?". Deploys
of `main` all report 0.1.0, so a version alone cannot tell a refreshed page
from a stale one - and finding that out by squinting at behaviour is exactly
how someone spends twenty minutes debugging a fix that never shipped.

So the commit as well, when it can be known. It is looked up once, cheaply, and
never blocks: an install that has no git, no checkout and no environment
variable simply reports its version and says nothing it cannot support.
"""

from __future__ import annotations

import os
import subprocess
from functools import lru_cache

from . import __version__

# How long to wait for git before giving up. This runs once at import, and a
# hung git - a network-backed working copy, a stale lock - must not hold up a
# server someone is waiting on. No answer is fine; a slow start is not.
_GIT_TIMEOUT_S = 2.0


@lru_cache(maxsize=1)
def build_id() -> str:
    """Short commit of what is running, or "" if it cannot be established.

    In order of trust: an explicit environment variable, then git in the
    working directory - which for the systemd service is the deployed checkout.
    A wheel installed somewhere with no repository has neither, and gets "".
    """
    explicit = (os.environ.get("COURSEOPS_BUILD") or "").strip()
    if explicit:
        return explicit[:40]

    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
            # Never let git prompt for anything, and never inherit a pager.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""                      # no git, or it misbehaved. Not a problem.
    if result.returncode != 0:
        return ""                      # not a checkout
    return result.stdout.strip()[:40]


def version_string() -> str:
    """What to show a human: the version, plus the build when we have one."""
    build = build_id()
    return f"{__version__} ({build})" if build else __version__
