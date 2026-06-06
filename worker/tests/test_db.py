# EE Gateway worker — tests for the SQLite packet store.
# Copyright (C) 2026 encryptedenergy.com
# Licensed under the GNU General Public License version 3 (GPL-3.0-only).
# See the LICENSE file at the repository root.

"""Tests for ee_gateway_worker.db.

Each test gets its own database under pytest's ``tmp_path``, so they are
isolated and leave nothing behind.
"""

from ee_gateway_worker import db


def _conn(tmp_path):
    return db.connect(tmp_path / "packets.db")


def test_connect_creates_file_and_enables_wal(tmp_path):
    dbfile = tmp_path / "nested" / "packets.db"  # parent dir does not exist yet
    conn = db.connect(dbfile)
    assert dbfile.exists()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_insert_returns_increasing_ids_and_stores_fields(tmp_path):
    conn = _conn(tmp_path)
    pid1 = db.insert_packet(conn, raw='{"n":1}', eid="ce38f7d1", rssi=-40)
    pid2 = db.insert_packet(conn, raw='{"n":2}', eid="ce38f7d1", rssi=-50)
    assert pid2 > pid1
    row = conn.execute("SELECT * FROM packet_log WHERE id=?", (pid1,)).fetchone()
    assert row["raw"] == '{"n":1}'
    assert row["eid"] == "ce38f7d1"
    assert row["rssi"] == -40
    assert row["ingested"] == 0
    assert row["scanned_at"] is not None


def test_insert_allows_null_eid_and_rssi(tmp_path):
    conn = _conn(tmp_path)
    pid = db.insert_packet(conn, raw="opaque")
    row = conn.execute("SELECT * FROM packet_log WHERE id=?", (pid,)).fetchone()
    assert row["eid"] is None
    assert row["rssi"] is None


def test_pending_packets_returns_uningested_oldest_first(tmp_path):
    conn = _conn(tmp_path)
    ids = [db.insert_packet(conn, raw=str(i)) for i in range(3)]
    assert [p["id"] for p in db.pending_packets(conn)] == ids


def test_pending_packets_respects_limit(tmp_path):
    conn = _conn(tmp_path)
    for i in range(5):
        db.insert_packet(conn, raw=str(i))
    assert len(db.pending_packets(conn, limit=3)) == 3


def test_mark_ingested_flips_status_and_clears_error(tmp_path):
    conn = _conn(tmp_path)
    pid = db.insert_packet(conn, raw="x", eid="abcd1234", rssi=-30)
    db.mark_ingest_error(conn, pid, "transient network failure")
    db.mark_ingested(conn, pid)
    row = conn.execute("SELECT * FROM packet_log WHERE id=?", (pid,)).fetchone()
    assert row["ingested"] == 1
    assert row["ingest_error"] is None
    assert row["ingest_at"] is not None
    assert db.pending_packets(conn) == []


def test_mark_ingest_error_keeps_packet_pending(tmp_path):
    conn = _conn(tmp_path)
    pid = db.insert_packet(conn, raw="x")
    db.mark_ingest_error(conn, pid, "boom")
    row = conn.execute("SELECT * FROM packet_log WHERE id=?", (pid,)).fetchone()
    assert row["ingested"] == 0
    assert row["ingest_error"] == "boom"
    assert [p["id"] for p in db.pending_packets(conn)] == [pid]


def test_stats_counts_total_ingested_pending_and_devices(tmp_path):
    conn = _conn(tmp_path)
    first = db.insert_packet(conn, raw="1", eid="dev-a")
    db.insert_packet(conn, raw="2", eid="dev-a")
    db.insert_packet(conn, raw="3", eid="dev-b")
    db.mark_ingested(conn, first)
    assert db.stats(conn) == {
        "total": 3,
        "ingested": 1,
        "pending": 2,
        "devices": 2,
    }


def test_stats_on_empty_database(tmp_path):
    conn = _conn(tmp_path)
    assert db.stats(conn) == {
        "total": 0,
        "ingested": 0,
        "pending": 0,
        "devices": 0,
    }


def test_device_summary_groups_by_eid_with_latest_rssi(tmp_path):
    conn = _conn(tmp_path)
    db.insert_packet(conn, raw="1", eid="dev-a", rssi=-40)
    db.insert_packet(conn, raw="2", eid="dev-a", rssi=-55)  # newer -> wins last_rssi
    db.insert_packet(conn, raw="3", eid="dev-b", rssi=-60)
    summary = {r["eid"]: r for r in db.device_summary(conn)}
    assert summary["dev-a"]["packets"] == 2
    assert summary["dev-a"]["last_rssi"] == -55
    assert summary["dev-a"]["last_seen"] >= summary["dev-a"]["first_seen"]
    assert summary["dev-b"]["packets"] == 1
    assert summary["dev-b"]["last_rssi"] == -60


def test_device_summary_excludes_null_eid(tmp_path):
    conn = _conn(tmp_path)
    db.insert_packet(conn, raw="anon")  # no eid
    db.insert_packet(conn, raw="named", eid="dev-a")
    eids = [r["eid"] for r in db.device_summary(conn)]
    assert eids == ["dev-a"]


# --- packet_type column ----------------------------------------------------

def test_insert_stores_packet_type(tmp_path):
    conn = _conn(tmp_path)
    pid = db.insert_packet(
        conn, raw="x", eid="dev-a", rssi=-40, packet_type="EncryptedPacket"
    )
    row = conn.execute("SELECT * FROM packet_log WHERE id=?", (pid,)).fetchone()
    assert row["packet_type"] == "EncryptedPacket"


def test_packet_type_defaults_to_null(tmp_path):
    conn = _conn(tmp_path)
    pid = db.insert_packet(conn, raw="x")
    row = conn.execute("SELECT * FROM packet_log WHERE id=?", (pid,)).fetchone()
    assert row["packet_type"] is None


def test_mark_skipped_leaves_pending_queue(tmp_path):
    conn = _conn(tmp_path)
    pid = db.insert_packet(conn, raw="x", packet_type="UnknownPacket")
    db.mark_skipped(conn, pid, "not ingestable")
    assert db.pending_packets(conn) == []          # gone from the queue
    row = conn.execute("SELECT * FROM packet_log WHERE id=?", (pid,)).fetchone()
    assert row["ingested"] == 1
    assert row["ingest_error"] == "not ingestable"  # skip reason retained
