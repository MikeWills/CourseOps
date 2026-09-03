"""Runtime settings, read from the environment (optionally seeded from .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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

    @classmethod
    def from_env(cls) -> "Settings":
        callsign = os.environ.get("APRS_CALLSIGN", "").strip().upper()
        # Two different mistakes with two different fixes. Telling someone who
        # has just copied the file to "copy the file" reads as the app not
        # noticing what they did, which is where a first setup stalls.
        if not callsign:
            raise SystemExit(
                "APRS_CALLSIGN is not set.\n"
                "  Copy .env.example to .env, then put your callsign in it:\n"
                "    APRS_CALLSIGN=W1AW"
            )
        if callsign == "N0CALL":
            raise SystemExit(
                "APRS_CALLSIGN is still the placeholder N0CALL.\n"
                "  Edit .env and put your own callsign there:\n"
                "    APRS_CALLSIGN=W1AW\n"
                "  It identifies this client to APRS-IS. Leave the passcode at\n"
                "  -1, which grants read access only - this never transmits."
            )
        return cls(
            callsign=callsign,
            passcode=os.environ.get("APRS_PASSCODE", "-1").strip(),
            host=os.environ.get("APRS_HOST", "rotate.aprs2.net").strip(),
            port=int(os.environ.get("APRS_PORT", "14580")),
            db_path=Path(os.environ.get("DB_PATH", "data/courseops.sqlite3")),
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        )
