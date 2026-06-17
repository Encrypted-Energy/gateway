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
import sys
import threading
import time

from hubblenetwork import ble
from hubblenetwork.errors import HubbleError

from ee_gateway_worker import config, counters, db, ee_client, gps, heartbeat

log = logging.getLogger("ee_gateway_worker")

# Paths inside the shared Umbrel app data directory. Overridable for local dev.
DATA_DIR = os.environ.get("EE_DATA_DIR", "/data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
# Sentinel file the UI touches when the user clicks "Restart worker" in the
# dashboard. The scan loop checks for this file once per cycle and, when
# present, deletes it and exits non-zero so Docker's restart policy
# (`restart: on-failure`) brings a fresh container up. See ui/app.py's
# /restart endpoint.
RESTART_SIGNAL_PATH = os.path.join(DATA_DIR, ".restart_requested")
DB_PATH = os.path.join(DATA_DIR, "packets.db")
STATE_PATH = os.path.join(DATA_DIR, "state.json")

# How often the ingest loop wakes to look for pending packets, and how many
# packets it sends per wake.
INGEST_POLL_SECONDS = 5
INGEST_BATCH = 50

# Housekeeping: delete already-ingested packets older than this and reclaim
# disk. Runs at most once per VACUUM_INTERVAL_SECONDS in the heartbeat loop.
PACKET_RETAIN_DAYS = 30
VACUUM_INTERVAL_SECONDS = 86_400  # 24h

# Set by the SIGTERM/SIGINT handler; both loops watch it and exit cleanly.
_stop = threading.Event()

# Process start time (monotonic), captured at import so reading uptime is
# constant-time and safe in any thread.
_PROCESS_START_MONOTONIC = time.monotonic()


def _uptime_seconds() -> int:
    return int(time.monotonic() - _PROCESS_START_MONOTONIC)


def _parse_optional_float(raw: str | None) -> float | None:
    """Return raw as a float, or None on empty / unparseable input. Used
    for env-var-driven optional config; a typo in the compose should not
    crash the worker."""
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        log.warning("ignoring unparseable float env var value: %r", raw)
        return None


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

    From 0.7.3 also forwards ``eid`` (the device identifier) so EE can
    enforce its per-device-per-org-per-day bounty cap server-side.
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
        # May be None for packet types that don't carry an EID. EE
        # treats absent / None eid as "skip the cap" which preserves
        # backward compat with pre-0.7.3 workers.
        "eid": fields.get("eid"),
    }


# --- restart signal --------------------------------------------------------

def _consume_restart_signal() -> bool:
    """Return True iff a restart was requested via the sentinel file.

    The UI's POST /restart handler touches ``RESTART_SIGNAL_PATH``; the scan
    loop polls this once per cycle. The file is deleted before we return
    True so the request is honored exactly once and a fresh worker boot is
    not stuck in a restart loop. Any filesystem error is swallowed: a
    non-removable sentinel is more usefully ignored than fatal.
    """
    if not os.path.exists(RESTART_SIGNAL_PATH):
        return False
    try:
        os.remove(RESTART_SIGNAL_PATH)
    except OSError as exc:
        log.warning("could not remove restart signal: %s", exc)
    return True


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
        # Honor the UI's "Restart worker" button, if pressed. The sentinel
        # file is consumed (deleted) before we exit, so a fresh boot is not
        # stuck in a restart loop. exit(1) triggers Docker's on-failure
        # restart policy, which is what brings the container back up.
        if _consume_restart_signal():
            log.info("UI restart signal received; exiting non-zero for docker restart")
            _stop.set()
            sys.exit(1)

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
            if _counters is not None:
                _counters.add_ble_scan_error()
            _write_state(conn, status="scan_error", error=str(exc))
            _stop.wait(cfg.scan_interval)
            continue

        if packets and _counters is not None:
            _counters.add_heard(len(packets))

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
                    eid=packet.get("eid"),
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
    last_vacuum = 0.0  # epoch seconds; 0 forces a check on the first iteration
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
                uptime_seconds=_uptime_seconds(),
            )

            # Housekeeping. The DELETE is cheap (index on ingested) so we
            # run it freely; VACUUM is expensive (rewrites the whole DB)
            # so we only call it when we actually deleted something.
            now = time.time()
            if now - last_vacuum > VACUUM_INTERVAL_SECONDS:
                try:
                    deleted = db.vacuum_old_rows(conn, retain_days=PACKET_RETAIN_DAYS)
                    if deleted:
                        log.info("vacuumed %d packets older than %dd",
                                 deleted, PACKET_RETAIN_DAYS)
                        conn.execute("VACUUM")
                except Exception as exc:  # noqa: BLE001
                    # Never crash the heartbeat loop on housekeeping
                    # failure; log and move on. Next 24h tick retries.
                    log.warning("vacuum failed: %s", exc)
                last_vacuum = now

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
    #
    # Fixed-location override (0.7.4+): both fixed_lat and fixed_lon may be
    # supplied via either the UI (/advanced -> config.json) or env vars
    # (EE_GPS_FIXED_LAT / LON, legacy 0.7.1 path). When both are set
    # in-range, the worker ignores gpsd and stamps every packet with the
    # configured coordinate. Use cases: kiosk / indoor mount where no GPS
    # signal is available, or development testing while a replacement
    # dongle ships. Reading from config.json (persistent /data volume)
    # is the recommended path because it survives Umbrel app updates,
    # which overwrite the compose file (and thus any env-var overrides).
    _counters = counters.CountersStore()
    try:
        _boot_config = config.load(CONFIG_PATH)
    except config.ConfigError:
        # Pre-setup: no credentials yet. Start GpsClient without fixed
        # coords; the scan loop's per-iteration config reload will pick
        # them up once the operator completes setup.
        _boot_config = None
    _gps = gps.GpsClient(
        fixed_lat=(_boot_config.fixed_lat if _boot_config else None),
        fixed_lon=(_boot_config.fixed_lon if _boot_config else None),
    )
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
