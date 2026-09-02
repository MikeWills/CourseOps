"""Async APRS-IS client.

One connection serves the whole application; browsers connect to our own
WebSocket and the server fans out. APRS-IS bans clients that open many
connections or reconnect in a tight loop, so reconnects are backed off.

Receive-only: passcode -1 grants read access and no transmit capability. The
club needs a callsign and no secret at all.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator

from . import __version__
from .config import APP_NAME

log = logging.getLogger(__name__)

# APRS-IS sends a '#' keepalive comment roughly every 20s. Well past that with
# nothing at all means the connection is wedged, not idle.
READ_TIMEOUT_SECONDS = 120
INITIAL_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 300.0

# Buddy filter entries per b/ clause. APRS-IS caps arguments per clause, so
# long rosters are split across several clauses.
BUDDIES_PER_CLAUSE = 20


def build_filter(station_keys: list[str], extra: str | None = None) -> str:
    """Server-side filter for a known roster.

    A buddy filter is the right tool here: the club knows its callsigns in
    advance, so we ask APRS-IS for exactly those stations. That cuts inbound
    traffic by orders of magnitude versus filtering client-side, and it avoids
    incidentally storing the location of the public.
    """
    clauses = []
    keys = [k.strip().upper() for k in station_keys if k and k.strip()]
    for i in range(0, len(keys), BUDDIES_PER_CLAUSE):
        chunk = keys[i:i + BUDDIES_PER_CLAUSE]
        clauses.append("b/" + "/".join(chunk))
    if extra and extra.strip():
        clauses.append(extra.strip())
    return " ".join(clauses)


def build_login(callsign: str, passcode: str, aprs_filter: str) -> str:
    login = (
        f"user {callsign} pass {passcode} "
        f"vers {APP_NAME} {__version__}"
    )
    if aprs_filter:
        login += f" filter {aprs_filter}"
    return login


async def stream_packets(
    host: str,
    port: int,
    callsign: str,
    passcode: str,
    aprs_filter: str,
) -> AsyncIterator[str]:
    """Yield raw APRS-IS lines forever, reconnecting with backoff.

    Server '#' comment lines are logged, not yielded.
    """
    backoff = INITIAL_BACKOFF_SECONDS

    while True:
        writer = None
        try:
            log.info("Connecting to APRS-IS %s:%s", host, port)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=30
            )

            banner = await asyncio.wait_for(reader.readline(), timeout=30)
            log.info("Server banner: %s", banner.decode("utf-8", "replace").strip())

            login = build_login(callsign, passcode, aprs_filter)
            # Never log the login line itself; it carries the passcode.
            log.info("Logging in as %s with filter: %s", callsign, aprs_filter or "(none)")
            writer.write((login + "\r\n").encode("utf-8"))
            await writer.drain()

            backoff = INITIAL_BACKOFF_SECONDS  # a real connection resets it

            while True:
                raw_line = await asyncio.wait_for(
                    reader.readline(), timeout=READ_TIMEOUT_SECONDS
                )
                if not raw_line:
                    raise ConnectionError("server closed the connection")

                line = raw_line.decode("utf-8", "replace").strip()
                if not line:
                    continue
                if line.startswith("#"):
                    log.debug("Server: %s", line)
                    continue
                yield line

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("APRS-IS connection lost (%s: %s)", type(exc).__name__, exc)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

        # Jitter so a server restart does not bring every client back at once.
        delay = min(backoff, MAX_BACKOFF_SECONDS) * (0.5 + random.random())
        log.info("Reconnecting in %.0fs", delay)
        await asyncio.sleep(delay)
        backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
