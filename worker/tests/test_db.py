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


def test_mark_ingested_many_clears_whole_batch(tmp_path):
    conn = _conn(tmp_path)
    ids = [db.insert_packet(conn, raw=str(i)) for i in range(4)]
    db.mark_ingest_error(conn, ids[0], "prior error")  # should be cleared
    db.mark_ingested_many(conn, ids)
    rows = conn.execute("SELECT ingested, ingest_error FROM packet_log").fetchall()
    assert all(r["ingested"] == 1 and r["ingest_error"] is None for r in rows)
    assert db.pending_packets(conn) == []


def test_mark_ingest_error_many_keeps_batch_pending(tmp_path):
    conn = _conn(tmp_path)
    ids = [db.insert_packet(conn, raw=str(i)) for i in range(3)]
    db.mark_ingest_error_many(conn, ids, "hubble down")
    rows = conn.execute("SELECT ingested, ingest_error FROM packet_log").fetchall()
    assert all(r["ingested"] == 0 and r["ingest_error"] == "hubble down" for r in rows)
    assert [p["id"] for p in db.pending_packets(conn)] == ids


def test_mark_skipped_many_drops_whole_batch(tmp_path):
    conn = _conn(tmp_path)
    ids = [db.insert_packet(conn, raw=str(i)) for i in range(3)]
    db.mark_skipped_many(conn, ids, "token revoked")
    rows = conn.execute("SELECT ingested, ingest_error FROM packet_log").fetchall()
    assert all(r["ingested"] == 1 and r["ingest_error"] == "token revoked" for r in rows)
    assert db.pending_packets(conn) == []


def test_batch_mark_helpers_are_noops_on_empty_list(tmp_path):
    conn = _conn(tmp_path)
    pid = db.insert_packet(conn, raw="x")
    db.mark_ingested_many(conn, [])
    db.mark_ingest_error_many(conn, [], "e")
    db.mark_skipped_many(conn, [], "r")
    # The one real row is untouched and still pending.
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


# --- device_summary_window (used by 0.6.0+ heartbeat) -----------------------

def test_device_summary_window_excludes_packets_older_than_window(tmp_path):
    import time
    conn = _conn(tmp_path)
    now = time.time()
    # Three rows: two recent, one well outside the 1h window.
    db.insert_packet(conn, raw="old", eid="dev-old", rssi=-70,
                     scanned_at=now - 3600 * 5)
    db.insert_packet(conn, raw="r1", eid="dev-new", rssi=-40,
                     scanned_at=now - 60)
    db.insert_packet(conn, raw="r2", eid="dev-new", rssi=-45,
                     scanned_at=now - 30)
    rows = db.device_summary_window(conn, window_hours=1)
    eids = [r["eid"] for r in rows]
    assert "dev-old" not in eids
    assert eids == ["dev-new"]
    assert rows[0]["packets"] == 2
    assert rows[0]["last_rssi"] == -45  # newest within window


def test_device_summary_window_orders_by_packets_then_eid(tmp_path):
    import time
    conn = _conn(tmp_path)
    now = time.time()
    # b and c tie on packet count; secondary sort by eid must surface b first.
    db.insert_packet(conn, raw="x", eid="dev-c", rssi=-50, scanned_at=now - 10)
    db.insert_packet(conn, raw="x", eid="dev-c", rssi=-50, scanned_at=now - 9)
    db.insert_packet(conn, raw="x", eid="dev-b", rssi=-50, scanned_at=now - 8)
    db.insert_packet(conn, raw="x", eid="dev-b", rssi=-50, scanned_at=now - 7)
    db.insert_packet(conn, raw="x", eid="dev-a", rssi=-50, scanned_at=now - 6)
    rows = db.device_summary_window(conn, window_hours=1)
    assert [r["eid"] for r in rows] == ["dev-b", "dev-c", "dev-a"]


def test_device_summary_window_respects_limit(tmp_path):
    conn = _conn(tmp_path)
    for i in range(5):
        db.insert_packet(conn, raw="x", eid=f"dev-{i}", rssi=-50)
    rows = db.device_summary_window(conn, window_hours=24, limit=2)
    assert len(rows) == 2


def test_device_summary_window_excludes_null_eid(tmp_path):
    conn = _conn(tmp_path)
    db.insert_packet(conn, raw="anon")
    db.insert_packet(conn, raw="named", eid="dev-a")
    rows = db.device_summary_window(conn, window_hours=24)
    assert [r["eid"] for r in rows] == ["dev-a"]


def test_device_summary_window_returns_empty_on_fresh_db(tmp_path):
    conn = _conn(tmp_path)
    rows = db.device_summary_window(conn, window_hours=24)
    assert rows == []


# --- vacuum_old_rows (worker housekeeping) ---------------------------------

def test_vacuum_deletes_only_ingested_rows_older_than_retain_days(tmp_path):
    import time
    conn = _conn(tmp_path)
    old_ingested  = db.insert_packet(conn, raw="old-i",  eid="a", rssi=-40,
                                     scanned_at=time.time() - 86400 * 60)
    old_pending   = db.insert_packet(conn, raw="old-p",  eid="b", rssi=-40,
                                     scanned_at=time.time() - 86400 * 60)
    recent_ingested = db.insert_packet(conn, raw="new-i", eid="c", rssi=-40)
    db.mark_ingested(conn, old_ingested)
    db.mark_ingested(conn, recent_ingested)
    # Force the old ingested row's ingest_at into the past (mark_ingested
    # uses time.time() under the hood, which is "now").
    conn.execute("UPDATE packet_log SET ingest_at = ? WHERE id = ?",
                 (time.time() - 86400 * 60, old_ingested))
    conn.commit()

    deleted = db.vacuum_old_rows(conn, retain_days=30)
    assert deleted == 1

    remaining_ids = {r["id"] for r in conn.execute("SELECT id FROM packet_log")}
    assert old_ingested not in remaining_ids   # deleted
    assert old_pending in remaining_ids        # pending: never deleted
    assert recent_ingested in remaining_ids    # within retention


def test_vacuum_returns_zero_on_empty_db(tmp_path):
    assert db.vacuum_old_rows(_conn(tmp_path)) == 0


def test_vacuum_leaves_pending_packets_untouched(tmp_path):
    import time
    conn = _conn(tmp_path)
    pid = db.insert_packet(conn, raw="x", eid="a", rssi=-40,
                           scanned_at=time.time() - 86400 * 365)
    # Pending packet from a year ago must NOT be deleted, even though
    # it is far older than the retention window.
    assert db.vacuum_old_rows(conn, retain_days=30) == 0
    assert conn.execute("SELECT COUNT(*) FROM packet_log").fetchone()[0] == 1


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
