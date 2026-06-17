# EE Gateway worker — gpsd client.
# Copyright (C) 2026 encryptedenergy.com
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License version 3 as published
# by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for
# more details. You should have received a copy of the license in the LICENSE
# file at the repository root; if not, see <https://www.gnu.org/licenses/>.

"""Read the current GPS fix from a local ``gpsd`` over its JSON socket.

The worker bundles ``gpsd`` in its container image (see ``Dockerfile`` and
``entrypoint.sh``); this module talks to it over TCP at ``127.0.0.1:2947``
so we never pin a third-party gpsd Python library. A background thread
keeps the most recent TPV fix in memory; the scan loop polls
:meth:`GpsClient.current_fix` synchronously and either embeds the fix in
the outgoing packet or drops the packet (Hubble rejects packets without
coordinates anyway).

Status semantics:

* ``"dongle_missing"`` — gpsd reports no DEVICES, or we cannot reach gpsd.
* ``"no_fix"`` — gpsd is connected and sees a dongle, but the dongle has
  not produced a 2D fix yet (cold start, indoors, antenna unplugged).
* ``"fix"`` — we have a fresh 2D fix (mode >= 2) less than
  :data:`GPS_FIX_TTL_SECONDS` old.

All failures are non-fatal: if gpsd is restarted, killed, or the socket
disappears, we silently reconnect with backoff. The scan loop sees the
status flip to ``"no_fix"`` or ``"dongle_missing"`` and surfaces it to
the operator via the heartbeat.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("ee_gateway_worker.gps")

# A fix older than this is considered stale; status downgrades to "no_fix"
# even though the dongle is still connected. 30s is generous for a dongle
# that nominally emits TPV every second.
GPS_FIX_TTL_SECONDS = 30

# Wait this long between reconnect attempts when gpsd is unreachable. Short
# enough that an operator who just started gpsd sees the status flip
# quickly; long enough that we do not busy-loop.
RECONNECT_BACKOFF_SECONDS = 5

GPSD_HOST = "127.0.0.1"
GPSD_PORT = 2947


@dataclass(frozen=True)
class GpsFix:
    """A 2D GPS fix the scan loop can stamp onto a packet."""

    lat: float
    lon: float
    at: float  # unix epoch seconds when gpsd reported the fix


class GpsClient:
    """Thread-safe gpsd reader. Start it once at worker boot; never restart.

    The ``_run`` thread owns the socket. The scan loop only calls
    :meth:`current_fix` and :meth:`status`, both of which take the lock
    briefly and return immediately.

    Fixed-location override (0.7.1+): when ``fixed_lat`` and ``fixed_lon``
    are both supplied (typically from ``EE_GPS_FIXED_LAT`` /
    ``EE_GPS_FIXED_LON`` env vars in the compose), the client reports a
    constant fix at that coordinate and stops paying attention to gpsd.
    Useful for stationary gateways (kiosks, indoor mounts) and for
    operators waiting on a replacement dongle to keep the ingest pipeline
    exercised. The gpsd background thread still starts so the gateway
    container looks identical, but its reads are ignored.
    """

    def __init__(
        self,
        host: str = GPSD_HOST,
        port: int = GPSD_PORT,
        fixed_lat: Optional[float] = None,
        fixed_lon: Optional[float] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._lock = threading.Lock()
        self._fix: Optional[GpsFix] = None
        self._dongle_seen = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="gpsd", daemon=True)
        # Fixed-location override. Both coords must be supplied; one alone
        # is invalid and is silently ignored (we log a warning when wiring
        # this up in main.py so the operator sees the misconfig).
        self._fixed: Optional[GpsFix] = None
        if fixed_lat is not None and fixed_lon is not None:
            self._fixed = GpsFix(lat=float(fixed_lat), lon=float(fixed_lon), at=time.time())
            log.warning(
                "GpsClient in FIXED-LOCATION mode at (%s, %s); gpsd reports will be ignored",
                fixed_lat, fixed_lon,
            )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def current_fix(self) -> Optional[GpsFix]:
        """Return the most recent non-stale fix, or ``None`` if no fresh fix.

        In fixed-location mode, returns a synthetic fix stamped at the
        current time on every call — never stale, never None.
        """
        if self._fixed is not None:
            # Refresh the timestamp so packets carry "now" and the fix
            # never trips the TTL check elsewhere in the codebase.
            return GpsFix(lat=self._fixed.lat, lon=self._fixed.lon, at=time.time())
        with self._lock:
            if self._fix is None:
                return None
            if time.time() - self._fix.at > GPS_FIX_TTL_SECONDS:
                return None
            return self._fix

    def status(self) -> str:
        """One of ``"fix"``, ``"no_fix"``, ``"dongle_missing"``."""
        if self._fixed is not None:
            return "fix"
        with self._lock:
            if not self._dongle_seen:
                return "dongle_missing"
            if self._fix is None or time.time() - self._fix.at > GPS_FIX_TTL_SECONDS:
                return "no_fix"
            return "fix"

    # --- internal --------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._read_forever()
            except (OSError, socket.timeout) as exc:
                log.debug("gpsd connection ended: %s", exc)
            # Backoff before retrying; honour stop while we wait so shutdown
            # is responsive.
            self._stop.wait(RECONNECT_BACKOFF_SECONDS)

    def _read_forever(self) -> None:
        with socket.create_connection((self._host, self._port), timeout=5) as sock:
            sock.settimeout(15)
            # Subscribe to JSON reports for all devices.
            sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
            buf = b""
            while not self._stop.is_set():
                chunk = sock.recv(4096)
                if not chunk:
                    return  # gpsd closed the socket; outer loop reconnects
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._handle_line(line)

    def _handle_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            return
        cls = msg.get("class")
        if cls == "DEVICES":
            # DEVICES is the canonical "what dongles does gpsd see" message.
            with self._lock:
                self._dongle_seen = bool(msg.get("devices"))
        elif cls == "DEVICE":
            # Sent when a device is added/removed.
            with self._lock:
                self._dongle_seen = True
        elif cls == "TPV":
            mode = msg.get("mode", 0)
            lat = msg.get("lat")
            lon = msg.get("lon")
            # mode >= 2 means 2D fix; mode 1 is "no fix" and we ignore the
            # (often present but garbage) lat/lon fields in that case.
            if mode >= 2 and lat is not None and lon is not None:
                with self._lock:
                    self._dongle_seen = True
                    self._fix = GpsFix(lat=float(lat), lon=float(lon), at=time.time())
