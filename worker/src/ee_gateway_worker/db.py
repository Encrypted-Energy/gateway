# EE Gateway worker — SQLite packet store.
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

"""Durable storage for scanned Hubble BLE packets.

One table, ``packet_log``, holds every scanned packet. A packet is inserted
with ``ingested = 0``; the ingest loop flips it to ``1`` once the Hubble cloud
has accepted it, or records an error and leaves it pending for retry.

Device-level stats for the dashboard are computed on read with a ``GROUP BY``
over ``eid``, so there is no second table to keep consistent.

This module is deliberately agnostic about the shape of a Hubble packet. The
full packet is stored verbatim in the ``raw`` column (the caller decides the
encoding — JSON is expected). The worker also extracts three optional columns
so the dashboard can group and sort without parsing ``raw``: ``eid`` and
``rssi`` (signal info) and ``packet_type`` (the SDK class name). Any of the
three may be NULL — not every Hubble packet type carries an EID.

The worker writes this database; the UI container reads it. WAL mode plus a
connection busy timeout makes that one-writer / many-readers pattern safe.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS packet_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at   REAL    NOT NULL,
    eid          TEXT,
    rssi         INTEGER,
    packet_type  TEXT,
    raw          TEXT    NOT NULL,
    ingested     INTEGER NOT NULL DEFAULT 0,
    ingest_error TEXT,
    ingest_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_packet_log_pending ON packet_log (ingested);
CREATE INDEX IF NOT EXISTS idx_packet_log_eid     ON packet_log (eid);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the packet database, creating the file and schema if needed.

    The parent directory is created if absent. The connection is returned with
    ``sqlite3.Row`` rows, WAL journaling, and a 5-second busy timeout so a
    concurrent reader/writer waits rather than raising ``database is locked``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def insert_packet(
    conn: sqlite3.Connection,
    *,
    raw: str,
    eid: str | None = None,
    rssi: int | None = None,
    packet_type: str | None = None,
    scanned_at: float | None = None,
) -> int:
    """Store a freshly scanned packet as pending (``ingested = 0``).

    ``packet_type`` is the SDK class name of the scanned object (e.g.
    ``"EncryptedPacket"``); the worker fills it so the dashboard can show what
    kind of packet was seen without parsing ``raw``.

    ``scanned_at`` defaults to the current time. Returns the new row id.
    """
    if scanned_at is None:
        scanned_at = time.time()
    cur = conn.execute(
        "INSERT INTO packet_log (scanned_at, eid, rssi, packet_type, raw) "
        "VALUES (?, ?, ?, ?, ?)",
        (scanned_at, eid, rssi, packet_type, raw),
    )
    conn.commit()
    return int(cur.lastrowid)


def pending_packets(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    """Return packets not yet accepted by the Hubble cloud, oldest first."""
    return conn.execute(
        "SELECT * FROM packet_log WHERE ingested = 0 ORDER BY id ASC LIMIT ?",
        (limit,),
    ).fetchall()


def mark_ingested(conn: sqlite3.Connection, packet_id: int) -> None:
    """Mark a packet as successfully ingested and clear any prior error."""
    conn.execute(
        "UPDATE packet_log SET ingested = 1, ingest_error = NULL, ingest_at = ? "
        "WHERE id = ?",
        (time.time(), packet_id),
    )
    conn.commit()


def mark_ingest_error(conn: sqlite3.Connection, packet_id: int, error: str) -> None:
    """Record a failed ingest attempt; the packet stays pending for retry."""
    conn.execute(
        "UPDATE packet_log SET ingest_error = ?, ingest_at = ? WHERE id = ?",
        (error, time.time(), packet_id),
    )
    conn.commit()


def mark_skipped(conn: sqlite3.Connection, packet_id: int, reason: str) -> None:
    """Mark a packet as terminally done without sending it to the cloud.

    Used for packet types the Hubble cloud cannot accept. The row leaves the
    pending queue (``ingested = 1``) so it is never retried; ``reason`` is kept
    in ``ingest_error`` so the dashboard can tell a skip from a real send.
    """
    conn.execute(
        "UPDATE packet_log SET ingested = 1, ingest_error = ?, ingest_at = ? "
        "WHERE id = ?",
        (reason, time.time(), packet_id),
    )
    conn.commit()


def stats(conn: sqlite3.Connection) -> dict:
    """Return overall counts for the dashboard header.

    Keys: ``total``, ``ingested``, ``pending``, ``devices`` (distinct EIDs).
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       COALESCE(SUM(ingested), 0) AS ingested, "
        "       COUNT(DISTINCT eid) AS devices "
        "FROM packet_log"
    ).fetchone()
    total, ingested = row["total"], row["ingested"]
    return {
        "total": total,
        "ingested": ingested,
        "pending": total - ingested,
        "devices": row["devices"],
    }


def device_summary(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    """Return a per-EID rollup for the dashboard, most recently seen first.

    Columns: ``eid``, ``packets``, ``first_seen``, ``last_seen``, ``last_rssi``.
    Rows with a NULL ``eid`` are excluded.
    """
    return conn.execute(
        "SELECT p1.eid AS eid, "
        "       COUNT(*) AS packets, "
        "       MIN(p1.scanned_at) AS first_seen, "
        "       MAX(p1.scanned_at) AS last_seen, "
        "       (SELECT p2.rssi FROM packet_log p2 "
        "        WHERE p2.eid = p1.eid ORDER BY p2.id DESC LIMIT 1) AS last_rssi "
        "FROM packet_log p1 "
        "WHERE p1.eid IS NOT NULL "
        "GROUP BY p1.eid "
        "ORDER BY last_seen DESC "
        "LIMIT ?",
        (limit,),
    ).fetchall()


def vacuum_old_rows(
    conn: sqlite3.Connection, *, retain_days: int = 30
) -> int:
    """Delete already-ingested packets older than ``retain_days`` and return
    the row count removed.

    Safety net for unbounded growth of ``packet_log``: a busy gateway at
    ~1 packet/sec produces ~2.6M rows/month, and the 24h
    :func:`device_summary_window` scan starts to slow once the table
    crosses the low millions. We only delete rows where
    ``ingested = 1`` so the ingest loop's retry queue is never touched.

    Caller is responsible for running ``VACUUM`` afterwards if it wants
    the disk space back (we skip VACUUM when nothing was deleted, since
    it rewrites the whole DB file and is expensive on a Pi).
    """
    cutoff = time.time() - (retain_days * 86400)
    cur = conn.execute(
        "DELETE FROM packet_log "
        "WHERE ingested = 1 AND ingest_at IS NOT NULL AND ingest_at < ?",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


def device_summary_window(
    conn: sqlite3.Connection,
    *,
    window_hours: int = 24,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Per-EID rollup restricted to packets scanned in the last ``window_hours``.

    Returned columns: ``eid``, ``packets``, ``last_seen``, ``last_rssi``.
    Rows with a NULL ``eid`` are excluded. Sorted by ``packets`` desc with
    ``eid`` as a tiebreaker so the result is deterministic across heartbeats
    (callers POST this verbatim to the EE dashboard and ties otherwise
    cause the table to reshuffle between ticks).

    Used by the heartbeat loop to build the ``devices_seen`` payload.
    """
    since = time.time() - (window_hours * 3600)
    return conn.execute(
        "SELECT p1.eid AS eid, "
        "       COUNT(*) AS packets, "
        "       MAX(p1.scanned_at) AS last_seen, "
        "       (SELECT p2.rssi FROM packet_log p2 "
        "        WHERE p2.eid = p1.eid AND p2.scanned_at >= ? "
        "        ORDER BY p2.id DESC LIMIT 1) AS last_rssi "
        "FROM packet_log p1 "
        "WHERE p1.eid IS NOT NULL AND p1.scanned_at >= ? "
        "GROUP BY p1.eid "
        "ORDER BY packets DESC, eid ASC "
        "LIMIT ?",
        (since, since, limit),
    ).fetchall()
