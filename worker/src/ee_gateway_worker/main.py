# EE Gateway worker — scan loop and cloud ingest.
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

"""Gateway worker entry point.

Two threads share one WAL-mode SQLite database:

* the **scan loop** calls ``ble.scan()``, flattens each Hubble packet object
  into a plain row, and stores it as pending;
* the **ingest loop** drains pending rows and POSTs them to the Hubble cloud.

Splitting them means a slow cloud call never stalls BLE scanning.

Option A (see project notes): this file owns all knowledge of the Hubble SDK.
``db.py`` only sees plain values. ``_flatten`` turns an SDK packet object into
a row dict; ``_rebuild_encrypted`` does the reverse for the one packet type
the cloud accepts. The SDK ships four packet dataclasses with different
fields, and only ``EncryptedPacket`` is ingestable — the others are stored so
the dashboard can show them, then marked as not ingestable rather than left
pending forever.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import threading
import time

from hubblenetwork import ble
from hubblenetwork.errors import HubbleError

from ee_gateway_worker import config, counters, db, ee_client, gps, heartbeat

log = logging.getLogger("ee_gateway_worker")

# Paths inside the shared Umbrel app data directory. Overridable for local dev.
DATA_DIR = os.environ.get("EE_DATA_DIR", "/data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "packets.db")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

# How often the ingest loop wakes to look for pending packets, and how many
# packets it sends per wake.
INGEST_POLL_SECONDS = 5
INGEST_BATCH = 50

# Set by the SIGTERM/SIGINT handler; both loops watch it and exit cleanly.
_stop = threading.Event()

# Shared in-process singletons. Wired up in ``main()`` and passed to each
# loop; module-level so test code can patch them out if needed.
_gps: gps.GpsClient | None = None
_counters: counters.CountersStore | None = None


# --- packet flattening -----------------------------------------------------

def _flatten(packet, fix: gps.GpsFix | None = None) -> dict:
    """Turn an SDK packet object into a plain row dict for ``db.insert_packet``.

    Keys: ``raw`` (JSON string), ``eid`` (hex string or None), ``rssi``,
    ``packet_type`` (SDK class name). ``raw`` always carries the base64
    ``payload`` and ``timestamp``; when a GPS ``fix`` is passed in, ``raw``
    also carries the ``latitude`` and ``longitude`` at scan time. The fix
    is captured at scan time on purpose: a moving gateway (think Pi in a
    vehicle) needs the coordinate from when the packet was heard, not when
    we got around to forwarding it.
    """
    packet_type = type(packet).__name__
    payload = getattr(packet, "payload", b"") or b""
    fields = {
        "payload_b64": base64.b64encode(bytes(payload)).decode("ascii"),
        "rssi": getattr(packet, "rssi", None),
        "timestamp": getattr(packet, "timestamp", None),
        "protocol_version": getattr(packet, "protocol_version", None),
    }
    if fix is not None:
        fields["latitude"] = fix.lat
        fields["longitude"] = fix.lon
    # eid is an int on the packet types that have one; store it as hex so the
    # dashboard and the eid index get a stable string key.
    eid_int = getattr(packet, "eid", None)
    eid = format(eid_int, "x") if isinstance(eid_int, int) else None
    if eid is not None:
        fields["eid"] = eid
    return {
        "raw": json.dumps(fields),
        "eid": eid,
        "rssi": fields["rssi"],
        "packet_type": packet_type,
    }


def _rebuild_for_ee(raw: str) -> dict | None:
    """Reconstruct the fields EE's ingest endpoint needs from stored JSON.

    Returns ``None`` if the stored packet has no embedded coordinates (left
    over from a 0.4.0 worker, or stored before the first GPS fix). The
    caller should mark such rows skipped, not retry them — Hubble rejects
    packets without coordinates so re-sending will never succeed.
    """
    fields = json.loads(raw)
    lat = fields.get("latitude")
    lon = fields.get("longitude")
    if lat is None or lon is None:
        return None
    return {
        "payload_b64": fields["payload_b64"],
        "rssi": fields.get("rssi") or 0,
        "timestamp": fields.get("timestamp") or int(time.time()),
        "latitude": float(lat),
        "longitude": float(lon),
    }


# --- state file ------------------------------------------------------------

def _write_state(conn, *, status: str, error: str | None = None) -> None:
    """Write ``state.json`` for the UI container to read.

    Best effort: a failure here is logged but never crashes a loop.
    """
    try:
        counts = db.stats(conn)
        state = {
            "status": status,
            "error": error,
            "updated_at": time.time(),
            "packets": counts,
        }
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_PATH)  # atomic; the UI never sees a half file
    except OSError as exc:
        log.warning("could not write state file: %s", exc)


# --- scan loop -------------------------------------------------------------

def scan_loop() -> None:
    """Scan for Hubble packets and store each one as pending.

    Opens its own SQLite connection: sqlite3 forbids sharing a connection
    across threads, and WAL mode is built for one connection per thread.
    """
    conn = db.connect(DB_PATH)
    while not _stop.is_set():
        try:
            cfg = config.load(CONFIG_PATH)
        except config.ConfigError as exc:
            _write_state(conn, status="needs_setup", error=str(exc))
            _stop.wait(10)
            continue

        try:
            packets = ble.scan(timeout=cfg.scan_timeout)
        except HubbleError as exc:
            log.error("scan failed: %s", exc)
            _write_state(conn, status="scan_error", error=str(exc))
            _stop.wait(cfg.scan_interval)
            continue

        # Capture a single GPS fix per scan cycle. Within one scan window
        # (typically 10s) the gateway has not moved meaningfully, so reusing
        # one fix for every packet in the batch is both correct and cheap.
        fix = _gps.current_fix() if _gps is not None else None
        stored = 0
        dropped_no_fix = 0
        for packet in packets:
            if fix is None:
                # Hubble rejects packets without coordinates and we have no
                # graceful way to backfill a location later. Drop now, surface
                # the count in the heartbeat, and let the dashboard explain
                # why to the operator.
                dropped_no_fix += 1
                continue
            row = _flatten(packet, fix=fix)
            db.insert_packet(
                conn,
                raw=row["raw"],
                eid=row["eid"],
                rssi=row["rssi"],
                packet_type=row["packet_type"],
            )
            stored += 1
        if dropped_no_fix and _counters is not None:
            _counters.add_dropped_no_fix(dropped_no_fix)
        if stored:
            log.info("stored %d packet(s)", stored)
        if dropped_no_fix:
            log.info("dropped %d packet(s): no GPS fix", dropped_no_fix)
        _write_state(conn, status="running")
        _stop.wait(cfg.scan_interval)


# --- ingest loop -----------------------------------------------------------

def ingest_loop() -> None:
    """Drain pending packets to encryptedenergy.com for proxy ingest to Hubble.

    The worker no longer talks to Hubble directly. EE owns the wholesale
    Hubble relationship; the worker just POSTs each packet to
    ``${EE_BASE_URL}/api/v1/gateways/packets`` with its EE bearer token.

    Failure handling:

    * IngestTerminal (HTTP 4xx)  EE rejected the packet. Mark it skipped so
      it leaves the pending queue. Common cause: token revoked.
    * IngestTransient (network or 5xx)  leave the packet pending; the next
      pass retries.

    Opens its own SQLite connection (see ``scan_loop``).
    """
    conn = db.connect(DB_PATH)

    while not _stop.is_set():
        try:
            cfg = config.load(CONFIG_PATH)
        except config.ConfigError:
            _stop.wait(INGEST_POLL_SECONDS)
            continue

        for row in db.pending_packets(conn, limit=INGEST_BATCH):
            if _stop.is_set():
                break
            if row["packet_type"] != "EncryptedPacket":
                # Only EncryptedPacket can be ingested. Mark the rest terminal
                # so they leave the pending queue instead of being retried.
                db.mark_skipped(conn, row["id"], "not ingestable")
                continue

            packet = _rebuild_for_ee(row["raw"])
            if packet is None:
                # Stored before the gateway had a GPS fix, or by a 0.4.0
                # worker that did not embed coordinates. Either way the
                # packet is unforwardable; mark it skipped so it leaves
                # the pending queue.
                db.mark_skipped(conn, row["id"], "no coordinates stored")
                continue
            try:
                ee_client.ingest_packet(
                    base_url=cfg.ee_base_url,
                    api_token=cfg.api_token,
                    payload_b64=packet["payload_b64"],
                    rssi=packet["rssi"],
                    timestamp=packet["timestamp"],
                    latitude=packet["latitude"],
                    longitude=packet["longitude"],
                )
                db.mark_ingested(conn, row["id"])
                if _counters is not None:
                    _counters.add_forwarded()
            except ee_client.IngestUnauthorized as exc:
                # 401 specifically: token invalid or revoked. Drop the
                # packet and flip the worker into auth_error so the
                # dashboard surfaces the credential problem.
                db.mark_skipped(conn, row["id"], str(exc))
                log.warning("EE rejected packet %d: %s", row["id"], exc)
                _write_state(conn, status="auth_error", error=str(exc))
            except ee_client.IngestTerminal as exc:
                # Other 4xx (e.g. 422 malformed). Drop the packet but do
                # not change worker state: the credentials are fine, this
                # is a per-packet data issue we cannot resolve by retrying.
                db.mark_skipped(conn, row["id"], str(exc))
                log.warning("EE dropped packet %d: %s", row["id"], exc)
            except ee_client.IngestTransient as exc:
                # Network or 5xx. Leave pending; the next pass retries.
                db.mark_ingest_error(conn, row["id"], str(exc))
                log.warning("ingest failed for packet %d: %s", row["id"], exc)

        _stop.wait(INGEST_POLL_SECONDS)


# --- heartbeat loop --------------------------------------------------------

def heartbeat_loop() -> None:
    """Send a periodic alive signal to encryptedenergy.com.

    Opens its own SQLite connection (sqlite3 forbids sharing across
    threads, matching the scan/ingest pattern) so the heartbeat can
    include a top-N per-EID rollup over the last 24h. Reads the current
    config every iteration so a credential or interval change in
    config.json takes effect within one cycle. Skips silently when
    credentials are missing (setup wizard has not been completed yet),
    rather than crashing the loop. All HTTP failures are non-fatal and
    handled inside heartbeat.report().
    """
    conn = db.connect(DB_PATH)
    try:
        while not _stop.is_set():
            try:
                cfg = config.load(CONFIG_PATH)
            except config.ConfigError:
                # No credentials yet; just wait and try again. The scan
                # loop writes the user-facing state, so we do not
                # duplicate that here.
                _stop.wait(30)
                continue

            heartbeat.report(
                base_url=cfg.ee_base_url,
                api_token=cfg.api_token,
                gps=_gps,
                counters_store=_counters,
                db_conn=conn,
            )
            _stop.wait(cfg.heartbeat_interval)
    finally:
        conn.close()


# --- entry point -----------------------------------------------------------

def _handle_signal(signum, _frame) -> None:
    log.info("signal %d received, shutting down", signum)
    _stop.set()


def main() -> None:
    global _gps, _counters

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # The Hubble SDK talks to the cloud over httpx, which logs one INFO line
    # per request. Quiet it to WARNING so the gateway log shows our own events
    # instead of an "HTTP Request: ..." line for every packet ingested.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    conn = db.connect(DB_PATH)
    _write_state(conn, status="starting")

    # GPS reader runs in its own thread and is shared across the scan loop
    # (reads current fix) and the heartbeat loop (reports status string).
    _counters = counters.CountersStore()
    _gps = gps.GpsClient()
    _gps.start()

    ingest = threading.Thread(target=ingest_loop, name="ingest", daemon=True)
    ingest.start()
    beat = threading.Thread(target=heartbeat_loop, name="heartbeat", daemon=True)
    beat.start()
    try:
        scan_loop()  # runs on the main thread until _stop is set
    finally:
        _stop.set()
        if _gps is not None:
            _gps.stop()
        ingest.join(timeout=15)
        beat.join(timeout=5)
        _write_state(conn, status="stopped")
        conn.close()


if __name__ == "__main__":
    main()
