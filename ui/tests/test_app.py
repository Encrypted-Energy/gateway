# EE Gateway UI tests. GPL-3.0-only; see LICENSE at the repository root.

"""Tests for the EE Gateway UI.

These exercise the Flask app through its test client. Each test gets a fresh
temporary ``data_dir`` (pytest hands the same function-scoped ``tmp_path`` to
both the ``client`` fixture and the test function), so the setup wizard, the
dashboard, and every degraded-input path can be checked in isolation.

The packet database is built here with raw SQL rather than by importing the
worker. The UI container ships without the worker package installed, and the
``packet_log`` schema is a deliberate contract between the two halves: a test
that re-declares the schema fails loudly if that contract drifts.
"""

import json
import sqlite3

import pytest

from ee_gateway_ui.app import create_app

# The packet_log schema, re-declared. This MUST stay in step with
# worker/db.py; the duplication is intentional (see the module docstring).
_PACKET_LOG_DDL = """
    CREATE TABLE packet_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        scanned_at   REAL    NOT NULL,
        eid          TEXT,
        rssi         INTEGER,
        packet_type  TEXT,
        raw          TEXT    NOT NULL,
        ingested     INTEGER NOT NULL DEFAULT 0,
        ingest_error TEXT,
        ingest_at    REAL
    )
"""


def _make_packet_db(path, rows):
    """Build a worker-shaped packets.db at ``path`` and insert ``rows``.

    Each row is a dict; only ``scanned_at`` and ``raw`` are required, every
    other column defaults to something harmless. Rows are inserted in list
    order, so later rows get higher ``id`` values — which is what the UI's
    "newest packet" RSSI subquery keys on.
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(_PACKET_LOG_DDL)
        for row in rows:
            conn.execute(
                "INSERT INTO packet_log "
                "(scanned_at, eid, rssi, packet_type, raw, ingested) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["scanned_at"],
                    row.get("eid"),
                    row.get("rssi"),
                    row.get("packet_type"),
                    row.get("raw", "{}"),
                    row.get("ingested", 0),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _write_state(data_dir, state):
    """Drop a state.json into ``data_dir`` exactly as the worker would."""
    (data_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


@pytest.fixture
def client(tmp_path):
    """A Flask test client backed by an empty temporary data directory."""
    app = create_app(data_dir=tmp_path)
    app.config["TESTING"] = True
    return app.test_client()


# --------------------------------------------------------------------------
# Health check
# --------------------------------------------------------------------------

def test_healthz_returns_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.data == b"ok"


# --------------------------------------------------------------------------
# Setup wizard
# --------------------------------------------------------------------------

def test_index_unconfigured_shows_setup(client):
    """With no config.json the root route is the setup wizard."""
    response = client.get("/")
    assert response.status_code == 200
    assert b"Connect to Hubble" in response.data


def test_post_valid_config_redirects_and_persists(client, tmp_path):
    """A valid POST writes config.json and redirects back to the dashboard."""
    response = client.post(
        "/config",
        data={"org_id": "org-abc", "api_token": "tok-xyz"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")

    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved == {"org_id": "org-abc", "api_token": "tok-xyz"}


def test_post_blank_token_rejected_and_nothing_written(client, tmp_path):
    """A blank API token is a 400; no config file is created."""
    response = client.post(
        "/config",
        data={"org_id": "org-abc", "api_token": "   "},
    )
    assert response.status_code == 400
    assert b"required" in response.data
    assert not (tmp_path / "config.json").exists()


def test_setup_prefills_org_id_but_never_echoes_token(client, tmp_path):
    """/setup pre-fills a stored org ID but never reveals the saved token."""
    (tmp_path / "config.json").write_text(
        json.dumps({"org_id": "org-visible", "api_token": "tok-secret-9999"}),
        encoding="utf-8",
    )
    response = client.get("/setup")
    assert response.status_code == 200
    assert b"org-visible" in response.data
    assert b"tok-secret-9999" not in response.data


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

def test_index_configured_shows_dashboard(client, tmp_path):
    """Once credentials exist the root route becomes the dashboard."""
    (tmp_path / "config.json").write_text(
        json.dumps({"org_id": "org-abc", "api_token": "tok-xyz"}),
        encoding="utf-8",
    )
    response = client.get("/")
    assert response.status_code == 200
    assert b"Devices in range" in response.data


def test_dashboard_survives_missing_worker_files(client, tmp_path):
    """Configured, but no state.json and no packets.db: still renders."""
    (tmp_path / "config.json").write_text(
        json.dumps({"org_id": "org-abc", "api_token": "tok-xyz"}),
        encoding="utf-8",
    )
    response = client.get("/")
    assert response.status_code == 200
    assert b"Worker not reporting yet" in response.data
    assert b"No devices yet" in response.data


def test_dashboard_lists_devices_from_packet_db(client, tmp_path):
    """A device that appears in packets.db is rendered in the table."""
    (tmp_path / "config.json").write_text(
        json.dumps({"org_id": "org-abc", "api_token": "tok-xyz"}),
        encoding="utf-8",
    )
    _make_packet_db(
        tmp_path / "packets.db",
        [
            {"scanned_at": 1000.0, "eid": "a290802a", "rssi": -60, "raw": "{}"},
            {"scanned_at": 1100.0, "eid": "a290802a", "rssi": -53, "raw": "{}"},
        ],
    )
    response = client.get("/")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "a290802a" in body
    # Two packets for the one device, newest RSSI from the highest-id row.
    assert "-53" in body
    assert "No devices yet" not in body


def test_dashboard_shows_counts_from_state_file(client, tmp_path):
    """Headline counters are read straight out of state.json."""
    (tmp_path / "config.json").write_text(
        json.dumps({"org_id": "org-abc", "api_token": "tok-xyz"}),
        encoding="utf-8",
    )
    _write_state(
        tmp_path,
        {
            "status": "running",
            "error": None,
            "updated_at": 1700000000.0,
            "packets": {
                "total": 42,
                "ingested": 40,
                "pending": 2,
                "devices": 7,
            },
        },
    )
    response = client.get("/")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "Running" in body
    assert "42" in body
    assert "7" in body
