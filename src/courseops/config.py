"""Runtime settings, read from the environment (optionally seeded from .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import resources

APP_NAME = "Course Ops"


def load_dotenv(path: str | Path = ".env") -> None:
    """Populate os.environ from a .env file. Existing variables win.

    Deliberately tiny: keeping the dependency list to one package matters more
    for a club standing this up than supporting exotic .env syntax.
    """
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass(frozen=True)
class Settings:
    callsign: str
    passcode: str
    host: str
    port: int
    db_path: Path
    log_level: str

    @property
    def callsign_problem(self) -> str | None:
        """Why this callsign cannot be used to reach APRS-IS, or None.

        Checked where the callsign is USED rather than where settings are
        built, because most of the app does not need one. Serving the map,
        importing a course, building a roster and running the whole setup UI
        are all perfectly valid with no callsign at all - only the live APRS-IS
        connection needs it.

        Refusing to start without one made the Windows build useless: someone
        double-clicks the executable, has no .env because they have never seen
        one, and gets a console window that flashes and vanishes.
        """
        if not self.callsign:
            return (
                "APRS_CALLSIGN is not set, so there is no live tracking.\n"
                "  Put your callsign in a file called .env next to the app:\n"
                "    APRS_CALLSIGN=W1AW"
            )
        if self.callsign == "N0CALL":
            return (
                "APRS_CALLSIGN is still the placeholder N0CALL, so there is no\n"
                "  live tracking. Edit .env and put your own callsign there:\n"
                "    APRS_CALLSIGN=W1AW\n"
                "  It identifies this client to APRS-IS. Leave the passcode at\n"
                "  -1, which grants read access only - this never transmits."
            )
        return None

    def require_callsign(self) -> None:
        """Refuse to go on where a callsign is genuinely needed."""
        problem = self.callsign_problem
        if problem:
            raise SystemExit(problem)

    @classmethod
    def from_env(cls) -> "Settings":
        callsign = os.environ.get("APRS_CALLSIGN", "").strip().upper()
        return cls(
            callsign=callsign,
            passcode=os.environ.get("APRS_PASSCODE", "-1").strip(),
            host=os.environ.get("APRS_HOST", "rotate.aprs2.net").strip(),
            port=int(os.environ.get("APRS_PORT", "14580")),
            # Frozen builds put this under the user's application data
            # rather than beside the .exe - see resources.default_data_dir.
            db_path=Path(
                os.environ.get("DB_PATH")
                or resources.default_data_dir() / "courseops.sqlite3"
            ),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        )
