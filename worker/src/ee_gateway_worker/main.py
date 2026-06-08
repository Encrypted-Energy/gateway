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

from ee_gateway_worker import config, db, ee_client, heartbeat

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


# --- packet flattening -----------------------------------------------------

def _flatten(packet) -> dict:
    """Turn an SDK packet object into a plain row dict for ``db.insert_packet``.

    Keys: ``raw`` (JSON string), ``eid`` (hex string or None), ``rssi``,
    ``packet_type`` (SDK class name). ``raw`` always carries the base64
    ``payload`` and ``timestamp`` so the ingest loop can rebuild the packet.
    """
    packet_type = type(packet).__name__
    payload = getattr(packet, "payload", b"") or b""
    fields = {
        "payload_b64": base64.b64encode(bytes(payload)).decode("ascii"),
        "rssi": getattr(packet, "rssi", None),
        "timestamp": getattr(packet, "timestamp", None),
        "protocol_version": getattr(packet, "protocol_version", None),
    }
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


def _rebuild_for_ee(raw: str) -> dict:
    """Reconstruct the fields EE's ingest endpoint needs from stored JSON.

    The worker stores the SDK packet's raw fields and rebuilds the minimal
    shape EE's ``POST /api/v1/gateways/packets`` expects. Coordinates default
    to (90, 0) for parity with the Hubble SDK's placeholder. Gateway 0.5.0
    will replace these with live GPS reads.
    """
    fields = json.loads(raw)
    return {
        "payload_b64": fields["payload_b64"],
        "rssi": fields.get("rssi") or 0,
        "timestamp": fields.get("timestamp") or int(time.time()),
        "latitude": 90.0,
        "longitude": 0.0,
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

        for packet in packets:
            row = _flatten(packet)
            db.insert_packet(
                conn,
                raw=row["raw"],
                eid=row["eid"],
                rssi=row["rssi"],
                packet_type=row["packet_type"],
            )
        if packets:
            log.info("stored %d packet(s)", len(packets))
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

    Reads the current config every iteration so a credential or interval
    change in config.json takes effect within one cycle. Skips silently
    when credentials are missing (setup wizard has not been completed yet),
    rather than crashing the loop. All HTTP failures are non-fatal and
    handled inside heartbeat.report().
    """
    while not _stop.is_set():
        try:
            cfg = config.load(CONFIG_PATH)
        except config.ConfigError:
            # No credentials yet; just wait and try again. The scan loop
            # writes the user-facing state, so we do not duplicate that here.
            _stop.wait(30)
            continue

        heartbeat.report(base_url=cfg.ee_base_url, api_token=cfg.api_token)
        _stop.wait(cfg.heartbeat_interval)


# --- entry point -----------------------------------------------------------

def _handle_signal(signum, _frame) -> None:
    log.info("signal %d received, shutting down", signum)
    _stop.set()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # The Hubble SDK talks to the cloud over httpx, which logs one INFO line
    # per request. Quiet it to WARNING so the gateway log shows our own events
    # instead of an "HTTP Request: ..." line for every packet ingested.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    conn = db.connect(DB_PATH)
    _write_state(conn, status="starting")

    ingest = threading.Thread(target=ingest_loop, name="ingest", daemon=True)
    ingest.start()
    beat = threading.Thread(target=heartbeat_loop, name="heartbeat", daemon=True)
    beat.start()
    try:
        scan_loop()  # runs on the main thread until _stop is set
    finally:
        _stop.set()
        ingest.join(timeout=15)
        beat.join(timeout=5)
        _write_state(conn, status="stopped")
        conn.close()


if __name__ == "__main__":
    main()
