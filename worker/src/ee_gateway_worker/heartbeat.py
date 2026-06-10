# EE Gateway worker, heartbeat client.
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

"""Periodic check-in to encryptedenergy.com.

The worker POSTs to ``${EE_BASE_URL}/api/v1/gateways/heartbeat`` so the EE
dashboard shows the gateway as alive and (from Gateway 0.5.0) reports
GPS state and per-interval throughput counters. Authentication is a
single Bearer token: the EE-issued API token already stored in
``config.json``. The token alone identifies the gateway, so no gateway
id is sent in the URL or body.

Implemented with ``urllib.request`` from the standard library to avoid
adding a runtime dependency. All failures are logged at WARNING and never
raise: a missed heartbeat is a transient observability gap, not a reason
to stop scanning or ingesting. When the POST fails *and* we have already
snapshotted the counter deltas, we restore the snapshot so the next
heartbeat carries the missed counts.
"""

from __future__ import annotations

import datetime
import json
import logging
import sqlite3
import urllib.error
import urllib.request

from ee_gateway_worker import counters as counters_mod
from ee_gateway_worker import db as db_mod
from ee_gateway_worker import gps as gps_mod

HEARTBEAT_PATH = "/api/v1/gateways/heartbeat"
REQUEST_TIMEOUT_SECONDS = 10
DEVICES_WINDOW_HOURS = 24
DEVICES_LIMIT = 50

log = logging.getLogger("ee_gateway_worker.heartbeat")


def _utc_iso(epoch_seconds: float) -> str:
    """Format a unix epoch as the ``YYYY-MM-DDTHH:MM:SSZ`` shape the EE
    heartbeat endpoint expects. Uses an aware datetime to avoid the
    Python 3.12+ deprecation warning on ``utcfromtimestamp``.
    """
    return (
        datetime.datetime.fromtimestamp(epoch_seconds, tz=datetime.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _devices_seen_payload(conn: sqlite3.Connection) -> dict:
    """Build the ``devices_seen`` body field from the local packet store.

    Always returns a dict with a ``devices`` array (possibly empty), so a
    gateway that has heard nothing in the window still POSTs a snapshot.
    That distinguishes "no devices in the last 24h" from "worker too old
    to report devices_seen" on the dashboard.
    """
    rows = db_mod.device_summary_window(
        conn, window_hours=DEVICES_WINDOW_HOURS, limit=DEVICES_LIMIT
    )
    now_iso = datetime.datetime.now(tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {
        "as_of":         now_iso,
        "window_hours":  DEVICES_WINDOW_HOURS,
        "devices": [
            {
                "eid":          row["eid"],
                "packets":      row["packets"],
                "last_rssi":    row["last_rssi"],
                "last_seen_at": _utc_iso(row["last_seen"]),
            }
            for row in rows
        ],
    }


def report(
    *,
    base_url: str,
    api_token: str,
    last_packet_at: str | None = None,
    last_known_position_at: str | None = None,
    gps: gps_mod.GpsClient | None = None,
    counters_store: counters_mod.CountersStore | None = None,
    db_conn: sqlite3.Connection | None = None,
) -> dict | None:
    """Send one heartbeat. Returns the parsed JSON response, or ``None`` on failure.

    ``gps`` adds ``gps_status`` ("fix" | "no_fix" | "dongle_missing") and,
    when a fresh fix is available, ``last_known_position_at``.

    ``counters_store``, when provided, contributes
    ``packets_forwarded_delta`` and ``packets_dropped_no_fix_delta``. The
    snapshot is reset to zero atomically before the POST; on any failure
    response the snapshot is restored so the deltas are not lost.

    ``db_conn``, when provided, contributes ``devices_seen`` (top-N
    per-EID rollup over the last 24h). Caller must own the connection
    (sqlite3 forbids cross-thread sharing).

    The EE side ignores any of these fields it does not recognise, so
    this call works against 0.5.x, 0.7.x, and the older 0.4.x server.
    """
    url = base_url.rstrip("/") + HEARTBEAT_PATH

    body: dict = {}
    if last_packet_at:
        body["last_packet_at"] = last_packet_at

    # GPS reporting. Take the snapshot here so a fix arriving mid-POST is
    # counted in the next heartbeat, not this one.
    if gps is not None:
        body["gps_status"] = gps.status()
        fix = gps.current_fix()
        if fix is not None:
            body["last_known_position_at"] = _utc_iso(fix.at)
    elif last_known_position_at:
        body["last_known_position_at"] = last_known_position_at

    # Devices-seen snapshot. Always include when a db connection is
    # available, including the empty case: the EE dashboard renders
    # different copy for "no devices in window" vs "worker never reported"
    # (NULL column), and we want operators of 0.6.0+ workers to land in
    # the former bucket as soon as the worker first checks in.
    if db_conn is not None:
        try:
            body["devices_seen"] = _devices_seen_payload(db_conn)
        except sqlite3.Error as exc:
            log.warning("could not build devices_seen payload: %s", exc)

    # Counter deltas. Snapshotted *now* so a concurrent increment lands in
    # the next heartbeat. If anything goes wrong below we put them back.
    snapshot = counters_store.snapshot_and_reset() if counters_store is not None else None
    if snapshot is not None:
        body["packets_forwarded_delta"] = snapshot.forwarded
        body["packets_dropped_no_fix_delta"] = snapshot.dropped_no_fix

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ee-gateway-worker/heartbeat",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {"ok": True}
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # Token invalid or revoked. The deltas are bound to a credential
            # that no longer works, so restoring them is pointless — drop
            # them and let the operator fix the credential.
            log.warning("heartbeat rejected (401), token invalid or revoked")
        elif exc.code >= 500:
            # Transient server error: keep the counts for the next try.
            if counters_store is not None and snapshot is not None:
                counters_store.restore(snapshot)
            log.warning("heartbeat HTTP %d: %s", exc.code, exc.reason)
        else:
            # Other 4xx (404, 422, ...). Either the endpoint shape changed
            # or our payload is bad. Dropping the deltas avoids an infinite
            # loop of doomed retries.
            log.warning("heartbeat HTTP %d: %s", exc.code, exc.reason)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Network error: gateway has no internet, DNS broke, EE is down.
        # Restore so the next heartbeat carries the counts.
        if counters_store is not None and snapshot is not None:
            counters_store.restore(snapshot)
        log.warning("heartbeat network error: %s", exc)
        return None
    except (ValueError, json.JSONDecodeError) as exc:
        # The POST itself succeeded; only the response body was malformed.
        # Treat as success on the counter side: EE *did* record the deltas.
        log.warning("heartbeat response not JSON: %s", exc)
        return None
