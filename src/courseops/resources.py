"""Where the app's own files live, whether it is running from source or frozen.

`Path(__file__).with_name(...)` is correct everywhere except inside a PyInstaller
executable, where the package is unpacked to a temporary directory and
`__file__` points somewhere that does not contain the data files. Getting this
wrong produces an .exe that starts, serves a page with no stylesheet, and fails
to open its database - all without a useful error.

Kept in one tiny module so there is exactly one place that knows about being
frozen, rather than the question being re-answered wherever a path is needed.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True inside a PyInstaller build."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def package_file(name: str) -> Path:
    """A file shipped alongside the code: `schema.sql`, `static/app.js`.

    PyInstaller unpacks bundled data under `sys._MEIPASS`, and this package's
    files are added there under `courseops/` to mirror the source layout - so
    the same relative name works in both worlds.
    """
    if is_frozen():
        return Path(sys._MEIPASS) / "courseops" / name  # type: ignore[attr-defined]
    return Path(__file__).with_name(name)


def default_data_dir() -> Path:
    """Where the database goes when nothing says otherwise.

    From source this stays `data/` beside the working directory, which is what
    the docs and the deployment scripts assume.

    A frozen build is different: someone double-clicks the .exe from a Downloads
    folder or a USB stick, and writing the event's only record next to the
    executable is how a race gets lost. It goes to the user's own application
    data instead, which persists, is writable without administrator rights, and
    is somewhere a person can find and back up.
    """
    if not is_frozen():
        return Path("data")

    import os

    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "CourseOps"
    return Path.home() / ".courseops"
