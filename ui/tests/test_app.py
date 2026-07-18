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

From 0.6.0: setup is a two-step flow (credentials, then location). Tests that
need the dashboard to render write BOTH halves via ``_write_full_config``;
tests that need to exercise step-2 routing write only credentials.
``verify_credentials`` is monkeypatched in the ``client`` fixture so /config
POSTs don't actually try to reach encryptedenergy.com.
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


def _write_creds_only(data_dir, **overrides):
    """Write a config.json with credentials but no location (step-1 done).

    Includes a legacy org_id key, as any pre-0.6.4 install would — every
    test using this helper doubles as back-compat coverage for it."""
    cfg = {"org_id": "org-abc", "api_token": "tok-xyz"}
    cfg.update(overrides)
    (data_dir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


def _write_full_config(data_dir, **overrides):
    """Write a config.json with credentials AND location (setup complete)."""
    cfg = {
        "org_id": "org-abc",
        "api_token": "tok-xyz",
        "fixed_lat": 40.712082,
        "fixed_lon": -74.040900,
    }
    cfg.update(overrides)
    (data_dir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A Flask test client backed by an empty temporary data directory.

    ``verify_credentials`` is monkeypatched to always succeed by default so
    tests don't hit the real ee-web verify endpoint. Tests that exercise
    the verification path explicitly override it via ``monkeypatch.setattr``.
    """
    monkeypatch.setattr(
        "ee_gateway_ui.app.verify_credentials",
        lambda base_url, api_token, timeout=8: (True, None, {
            "ok": True,
            "gateway": {
                "name": "Test Gateway",
                "public_id": "ee_gw_test",
                "organization_id": "ee_org_test",
                "organization_name": "Test Org",
            },
        }),
    )
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
# Setup wizard step 1 (credentials)
# --------------------------------------------------------------------------

def test_index_unconfigured_shows_setup_step_1(client):
    """With no config.json the root route is the credentials form."""
    response = client.get("/")
    assert response.status_code == 200
    # Use form-field labels as the durable marker; the page heading copy
    # has been rebranded once already (Hubble -> Encrypted Energy) and a
    # test pinned to that wording silently rotted until 0.6.3.
    assert b"API token" in response.data
    assert b"Step 1 of 2" in response.data
    # The org-ID field was removed in 0.6.4 (token-only setup).
    assert b"Organization ID" not in response.data


def test_post_valid_config_redirects_to_step_2(client, tmp_path):
    """A valid POST writes config.json and redirects to setup step 2."""
    response = client.post(
        "/config",
        data={"api_token": "tok-xyz"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup/location")

    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "org_id" not in saved  # token-only setup from 0.6.4
    assert saved["api_token"] == "tok-xyz"
    assert isinstance(saved["saved_at"], int)
    # Step 1 alone does NOT set a location yet.
    assert "fixed_lat" not in saved


def test_post_valid_config_skips_step_2_when_location_already_set(client, tmp_path):
    """Reconfigure path: if location is already saved, skip step 2."""
    _write_full_config(tmp_path)
    response = client.post(
        "/config",
        data={"api_token": "tok-new"},
    )
    assert response.status_code == 302
    # No /setup/location detour — straight to the dashboard.
    assert response.headers["Location"].endswith("/")


def test_post_blank_token_rejected_and_nothing_written(client, tmp_path):
    """A blank API token is a 400; no config file is created."""
    response = client.post(
        "/config",
        data={"api_token": "   "},
    )
    assert response.status_code == 400
    assert b"required" in response.data
    assert not (tmp_path / "config.json").exists()


def test_post_bad_credentials_rejected_inline(tmp_path, monkeypatch):
    """A verify-failed POST renders the form with the verify error and 400."""
    # Override the default-success stub for this test.
    monkeypatch.setattr(
        "ee_gateway_ui.app.verify_credentials",
        lambda base_url, api_token, timeout=8: (
            False,
            "The API token was rejected. Check that the token matches the gateway page on encryptedenergy.com.",
            None,
        ),
    )
    app = create_app(data_dir=tmp_path)
    app.config["TESTING"] = True
    client = app.test_client()

    response = client.post(
        "/config",
        data={"api_token": "tok-wrong"},
    )
    assert response.status_code == 400
    assert b"rejected" in response.data
    # Critically: nothing persisted.
    assert not (tmp_path / "config.json").exists()


def test_post_verify_network_failure_rejected_inline(tmp_path, monkeypatch):
    """Network failure during verify is surfaced as a user-facing error."""
    monkeypatch.setattr(
        "ee_gateway_ui.app.verify_credentials",
        lambda base_url, api_token, timeout=8: (
            False,
            "Could not reach Encrypted Energy: network down. Check the gateway's internet connection.",
            None,
        ),
    )
    app = create_app(data_dir=tmp_path)
    app.config["TESTING"] = True
    client = app.test_client()

    response = client.post(
        "/config",
        data={"api_token": "tok-xyz"},
    )
    assert response.status_code == 400
    assert b"Could not reach" in response.data
    assert not (tmp_path / "config.json").exists()


def test_setup_has_no_org_field_and_never_echoes_token(client, tmp_path):
    """/setup shows no org-ID field (0.6.4+) and never reveals the token.

    The stored config here carries a legacy org_id (pre-0.6.4 install);
    it must neither render nor break the page."""
    _write_creds_only(tmp_path, org_id="org-legacy", api_token="tok-secret-9999")
    response = client.get("/setup")
    assert response.status_code == 200
    assert b"Organization ID" not in response.data
    assert b"org-legacy" not in response.data
    assert b"tok-secret-9999" not in response.data


# --------------------------------------------------------------------------
# Setup wizard step 2 (location)
# --------------------------------------------------------------------------

def test_index_with_creds_no_location_shows_setup_location(client, tmp_path):
    """Credentials saved but no location -> step 2 form on /."""
    _write_creds_only(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert b"Step 2 of 2" in response.data
    assert b"Where is this gateway" in response.data


def test_setup_location_get_renders_form(client, tmp_path):
    """GET /setup/location renders the form with empty fields."""
    _write_creds_only(tmp_path)
    response = client.get("/setup/location")
    assert response.status_code == 200
    assert b"Step 2 of 2" in response.data


def test_setup_location_get_bounces_to_step_1_when_no_creds(client, tmp_path):
    """Without credentials, /setup/location redirects to credentials form."""
    response = client.get("/setup/location")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup")


def test_setup_location_post_valid_saves_and_advances_to_dashboard(client, tmp_path):
    """A valid location POST writes config and redirects to /."""
    _write_creds_only(tmp_path)
    response = client.post(
        "/setup/location",
        data={"fixed_lat": "40.712082", "fixed_lon": "-74.040900"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")

    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved["fixed_lat"] == 40.712082
    assert saved["fixed_lon"] == -74.0409
    # Legacy org_id key (pre-0.6.4 install) preserved across the merge.
    assert saved["org_id"] == "org-abc"
    # Restart sentinel touched so worker picks up the new mode.
    assert (tmp_path / ".restart_requested").exists()


def test_setup_location_post_bounces_to_step_1_when_no_creds(client, tmp_path):
    """Without credentials, POST /setup/location redirects to credentials form."""
    response = client.post(
        "/setup/location",
        data={"fixed_lat": "40.7", "fixed_lon": "-74.0"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup")
    # Nothing written.
    assert not (tmp_path / "config.json").exists()


def test_setup_location_post_blank_is_error(client, tmp_path):
    """Both blank rejected with 400 (no clear path on setup step 2)."""
    _write_creds_only(tmp_path)
    response = client.post(
        "/setup/location",
        data={"fixed_lat": "", "fixed_lon": ""},
    )
    assert response.status_code == 400
    assert b"required" in response.data.lower()


def test_setup_location_post_lat_out_of_range_is_error(client, tmp_path):
    _write_creds_only(tmp_path)
    response = client.post(
        "/setup/location",
        data={"fixed_lat": "95.0", "fixed_lon": "0.0"},
    )
    assert response.status_code == 400
    assert b"latitude" in response.data.lower()


def test_setup_location_post_non_numeric_is_error(client, tmp_path):
    _write_creds_only(tmp_path)
    response = client.post(
        "/setup/location",
        data={"fixed_lat": "north", "fixed_lon": "west"},
    )
    assert response.status_code == 400
    assert b"decimal degrees" in response.data.lower()


# --------------------------------------------------------------------------
# Dashboard (only renders when BOTH creds and location are set)
# --------------------------------------------------------------------------

def test_index_configured_shows_dashboard(client, tmp_path):
    """Once both credentials and location exist, root is the dashboard."""
    _write_full_config(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert b"Devices in range" in response.data


def test_dashboard_survives_missing_worker_files(client, tmp_path):
    """Configured, but no state.json and no packets.db: still renders."""
    _write_full_config(tmp_path)
    response = client.get("/")
    assert response.status_code == 200
    assert b"Worker not reporting yet" in response.data
    assert b"No devices yet" in response.data


def test_dashboard_lists_devices_from_packet_db(client, tmp_path):
    """A device that appears in packets.db is rendered in the table."""
    _write_full_config(tmp_path)
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
    assert "-53" in body
    assert "No devices yet" not in body


def test_dashboard_shows_counts_from_state_file(client, tmp_path):
    """Headline counters are read straight out of state.json."""
    _write_full_config(tmp_path)
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


def test_dashboard_renders_thousand_separators_on_large_counts(client, tmp_path):
    """Counts in the thousands render with comma separators for readability."""
    _write_full_config(tmp_path)
    _write_state(tmp_path, {
        "status": "running",
        "packets": {"total": 16923, "ingested": 16921, "pending": 2, "devices": 6},
    })
    response = client.get("/")
    body = response.data.decode("utf-8")
    assert "16,923" in body
    assert "16,921" in body


def test_dashboard_renders_dash_for_missing_counts(client, tmp_path):
    """A None count renders as `-` (the worker hasn't reported yet)."""
    _write_full_config(tmp_path)
    response = client.get("/")
    body = response.data.decode("utf-8")
    # No state.json -> counts are None -> dashes
    assert body.count(">-<") >= 4   # one per KPI tile


def test_dashboard_packets_label_says_30_days(client, tmp_path):
    """KPI sublabel reflects the 30-day worker retention window."""
    _write_full_config(tmp_path)
    response = client.get("/")
    body = response.data.decode("utf-8")
    assert "captured in last 30 days" in body


def test_dashboard_shows_location_pill(client, tmp_path):
    """Dashboard always shows the location pill now (location is required)."""
    _write_full_config(tmp_path)
    response = client.get("/")
    body = response.data.decode("utf-8")
    assert "Location" in body
    assert "40.712082" in body
    assert "-74.0409" in body


# --------------------------------------------------------------------------
# Verifying-credentials optimistic state
# --------------------------------------------------------------------------

def test_dashboard_shows_verifying_when_creds_just_saved(client, tmp_path):
    """Within the verifying window, dashboard masks the worker stale state."""
    import time as _time
    _write_full_config(tmp_path, saved_at=int(_time.time()) - 5)
    _write_state(tmp_path, {"status": "needs_setup", "error": "missing"})

    response = client.get("/")
    body = response.data.decode("utf-8")
    assert "Verifying credentials" in body
    assert "missing" not in body
    assert "http-equiv=\"refresh\"" in body


def test_dashboard_skips_verifying_after_window_expires(client, tmp_path):
    """Once the 60-second window has passed, dashboard shows worker truth."""
    import time as _time
    _write_full_config(tmp_path, saved_at=int(_time.time()) - 120)
    _write_state(tmp_path, {"status": "needs_setup", "error": "missing creds"})

    response = client.get("/")
    body = response.data.decode("utf-8")
    assert "Verifying credentials" not in body
    assert "Waiting for credentials" in body


def test_dashboard_skips_verifying_when_worker_already_running(client, tmp_path):
    """If worker is healthy, never show the optimistic state."""
    import time as _time
    _write_full_config(tmp_path, saved_at=int(_time.time()))
    _write_state(tmp_path, {
        "status": "running",
        "packets": {"total": 1, "ingested": 1, "pending": 0, "devices": 1},
    })

    response = client.get("/")
    body = response.data.decode("utf-8")
    assert "Verifying credentials" not in body
    assert "Running" in body


# --------------------------------------------------------------------------
# Restart-worker button
# --------------------------------------------------------------------------

def test_restart_touches_sentinel_and_redirects(client, tmp_path):
    """POST /restart writes the sentinel file the worker polls for."""
    response = client.post("/restart")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")
    assert (tmp_path / ".restart_requested").exists()


def test_restart_button_only_appears_on_dashboard(client, tmp_path):
    """The button is on the dashboard, not the setup wizard."""
    _write_full_config(tmp_path)
    response = client.get("/")
    assert b"Restart worker" in response.data

    response = client.get("/setup")
    assert b"Restart worker" not in response.data


# --------------------------------------------------------------------------
# /settings/location (renamed from /advanced in 0.6.0)
# --------------------------------------------------------------------------

def test_settings_location_get_renders_empty_when_no_override(client, tmp_path):
    """GET /settings/location shows the form with blank fields when nothing set."""
    response = client.get("/settings/location")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert "Gateway location" in body


def test_settings_location_get_prefills_when_set(client, tmp_path):
    """GET /settings/location prefills the form from config.json."""
    _write_full_config(tmp_path)
    response = client.get("/settings/location")
    body = response.data.decode("utf-8")
    assert "40.712082" in body
    assert "-74.0409" in body


def test_settings_location_post_valid_saves_and_restarts(client, tmp_path):
    """Valid lat/lon writes to config.json and touches the restart sentinel."""
    _write_creds_only(tmp_path)
    response = client.post("/settings/location", data={
        "fixed_lat": "40.712082",
        "fixed_lon": "-74.040900",
    })
    assert response.status_code == 200
    assert b"Saved" in response.data

    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert stored["fixed_lat"] == 40.712082
    assert stored["fixed_lon"] == -74.0409
    assert stored["org_id"] == "org-abc"
    assert stored["api_token"] == "tok-xyz"
    assert (tmp_path / ".restart_requested").exists()


def test_settings_location_post_blank_both_clears(client, tmp_path):
    """Both fields empty -> override removed from config.json."""
    _write_full_config(tmp_path)
    response = client.post("/settings/location", data={"fixed_lat": "", "fixed_lon": ""})
    assert response.status_code == 200

    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "fixed_lat" not in stored
    assert "fixed_lon" not in stored
    assert stored["org_id"] == "org-abc"


def test_settings_location_post_one_blank_is_error(client, tmp_path):
    """Lat without lon (or vice versa) is a 400."""
    response = client.post("/settings/location", data={"fixed_lat": "40.7", "fixed_lon": ""})
    assert response.status_code == 400
    assert b"required" in response.data.lower()


def test_settings_location_post_non_numeric_is_error(client, tmp_path):
    response = client.post("/settings/location", data={"fixed_lat": "north", "fixed_lon": "west"})
    assert response.status_code == 400
    assert b"decimal degrees" in response.data.lower()


def test_settings_location_post_lat_out_of_range_is_error(client, tmp_path):
    response = client.post("/settings/location", data={"fixed_lat": "95.0", "fixed_lon": "0.0"})
    assert response.status_code == 400
    assert b"latitude" in response.data.lower()


def test_settings_location_post_lon_out_of_range_is_error(client, tmp_path):
    response = client.post("/settings/location", data={"fixed_lat": "0.0", "fixed_lon": "-181.0"})
    assert response.status_code == 400
    assert b"longitude" in response.data.lower()


def test_settings_location_post_zero_zero_is_error(client, tmp_path):
    """(0.0, 0.0) is off the coast of Africa. A real gateway is never
    there, but it's the classic 'unset override' tell that used to land
    silently in config.json and cause every packet to be rejected
    upstream with an opaque 422. Reject it at the parser level with a
    specific error the operator can act on."""
    response = client.post(
        "/settings/location", data={"fixed_lat": "0.0", "fixed_lon": "0.0"}
    )
    assert response.status_code == 400
    assert b"cannot both be zero" in response.data.lower()


# --------------------------------------------------------------------------
# /advanced backward-compat redirect
# --------------------------------------------------------------------------

def test_advanced_get_redirects_to_settings_location(client, tmp_path):
    """Old /advanced bookmarks land on /settings/location via 308."""
    response = client.get("/advanced", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["Location"].endswith("/settings/location")


def test_advanced_post_redirects_to_settings_location(client, tmp_path):
    """308 preserves POST so a save against the old URL still lands correctly."""
    response = client.post("/advanced",
                           data={"fixed_lat": "40.7", "fixed_lon": "-74.0"},
                           follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["Location"].endswith("/settings/location")


# --------------------------------------------------------------------------
# Coordinate paste formats (0.6.4)
# --------------------------------------------------------------------------
# The form hint tells operators to right-click in Google Maps and copy the
# coordinates — which lands "40.712082, -74.040900" on the clipboard as ONE
# string. Before 0.6.4 pasting that into a field bounced with "must be
# numeric" on the majority setup path. These tests pin every real-world
# paste format the parser now accepts.

def _saved_coords(tmp_path):
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    return cfg["fixed_lat"], cfg["fixed_lon"]


def test_location_accepts_google_maps_pair_in_lat_field(client, tmp_path):
    """The full 'lat, lon' pair pasted into latitude, longitude left blank."""
    response = client.post(
        "/settings/location",
        data={"fixed_lat": "40.712082, -74.040900", "fixed_lon": ""},
    )
    assert response.status_code == 200
    assert _saved_coords(tmp_path) == (40.712082, -74.0409)


def test_setup_location_pair_paste_beats_prefilled_longitude(client, tmp_path):
    """Pair pasted into latitude while longitude still holds the North-Pole
    prefill ('0.0') — the pasted pair must win over the placeholder."""
    _write_creds_only(tmp_path)
    response = client.post(
        "/setup/location",
        data={"fixed_lat": "40.712082, -74.040900", "fixed_lon": "0.0"},
    )
    assert response.status_code == 302
    assert _saved_coords(tmp_path) == (40.712082, -74.0409)


def test_location_accepts_degree_symbol_and_hemisphere_letters(client, tmp_path):
    """Wikipedia / Google Earth style: degree symbol + N/S/E/W letters.
    W flips the longitude sign."""
    response = client.post(
        "/settings/location",
        data={"fixed_lat": "40.7128° N", "fixed_lon": "74.0409° W"},
    )
    assert response.status_code == 200
    assert _saved_coords(tmp_path) == (40.7128, -74.0409)


def test_location_accepts_unicode_minus(client, tmp_path):
    """Wikipedia renders negatives with U+2212, not ASCII '-'."""
    response = client.post(
        "/settings/location",
        data={"fixed_lat": "40.7128", "fixed_lon": "−74.0409"},
    )
    assert response.status_code == 200
    assert _saved_coords(tmp_path) == (40.7128, -74.0409)


def test_location_accepts_eu_decimal_commas(client, tmp_path):
    """'40,7128' with a filled other field is a decimal comma, NOT a pair."""
    response = client.post(
        "/settings/location",
        data={"fixed_lat": "40,7128", "fixed_lon": "-74,0409"},
    )
    assert response.status_code == 200
    assert _saved_coords(tmp_path) == (40.7128, -74.0409)


def test_location_accepts_trailing_comma_from_hand_split_pair(client, tmp_path):
    """Operator split the Google Maps pair by hand and left the comma."""
    response = client.post(
        "/settings/location",
        data={"fixed_lat": "40.712082,", "fixed_lon": "-74.040900"},
    )
    assert response.status_code == 200
    assert _saved_coords(tmp_path) == (40.712082, -74.0409)


def test_location_still_rejects_dms_format(client, tmp_path):
    """Degrees-minutes-seconds is not supported; the error shows the
    decimal format to use instead."""
    response = client.post(
        "/settings/location",
        data={"fixed_lat": "40°42'43\"N", "fixed_lon": "74°02'27\"W"},
    )
    assert response.status_code == 400
    assert b"decimal degrees" in response.data.lower()
    assert not (tmp_path / "config.json").exists()


def test_location_rejects_zero_zero_pair_paste(client, tmp_path):
    """(0, 0) pasted as a pair hits the same unset-tell rejection as
    (0, 0) entered field by field."""
    response = client.post(
        "/settings/location",
        data={"fixed_lat": "0.0, 0.0", "fixed_lon": ""},
    )
    assert response.status_code == 400
    assert b"cannot both be zero" in response.data.lower()


# --------------------------------------------------------------------------
# Address geocoding (0.6.4)
# --------------------------------------------------------------------------

_GEOCODE_HIT = {
    "lat": 40.748441,
    "lon": -73.985664,
    "display_name": "Empire State Building, 350, 5th Avenue, New York",
}


def _stub_geocode(monkeypatch, result=(True, None, _GEOCODE_HIT)):
    calls = []

    def fake(query, timeout=10):
        calls.append(query)
        return result

    monkeypatch.setattr("ee_gateway_ui.app.geocode_address", fake)
    return calls


def test_settings_lookup_fills_fields_without_saving(client, tmp_path, monkeypatch):
    """The 'Find coordinates' submit geocodes and re-renders for review;
    nothing is persisted and the worker is not restarted."""
    calls = _stub_geocode(monkeypatch)
    response = client.post(
        "/settings/location",
        data={"action": "lookup", "address": "350 5th Ave, New York"},
    )
    assert response.status_code == 200
    assert calls == ["350 5th Ave, New York"]
    assert b"40.748441" in response.data
    assert b"-73.985664" in response.data
    assert b"Empire State Building" in response.data
    assert not (tmp_path / "config.json").exists()
    assert not (tmp_path / ".restart_requested").exists()


def test_settings_lookup_failure_shows_error_and_manual_path(client, tmp_path, monkeypatch):
    _stub_geocode(
        monkeypatch,
        result=(False, "No match for that address. Add a city or country and "
                       "try again. You can also enter the coordinates manually "
                       "below.", None),
    )
    response = client.post(
        "/settings/location",
        data={"action": "lookup", "address": "asdfghjkl"},
    )
    assert response.status_code == 400
    assert b"No match" in response.data
    assert b"manually" in response.data
    assert not (tmp_path / "config.json").exists()


def test_setup_lookup_then_save_flow(client, tmp_path, monkeypatch):
    """Setup step 2: lookup fills the fields, the follow-up save persists
    them and advances to the dashboard."""
    _write_creds_only(tmp_path)
    _stub_geocode(monkeypatch)
    lookup = client.post(
        "/setup/location",
        data={"action": "lookup", "address": "350 5th Ave, New York"},
    )
    assert lookup.status_code == 200
    assert b"40.748441" in lookup.data

    save = client.post(
        "/setup/location",
        data={"fixed_lat": "40.748441", "fixed_lon": "-73.985664"},
    )
    assert save.status_code == 302
    assert _saved_coords(tmp_path) == (40.748441, -73.985664)
    assert (tmp_path / ".restart_requested").exists()


def test_geocode_address_parses_nominatim_response(monkeypatch):
    """Unit test against a canned Nominatim payload (no network)."""
    from ee_gateway_ui import app as app_module

    class FakeResponse:
        def __init__(self, body):
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    body = json.dumps([{
        "lat": "40.748441", "lon": "-73.985664",
        "display_name": "Empire State Building, New York",
    }]).encode("utf-8")
    monkeypatch.setattr(
        app_module.urllib.request, "urlopen",
        lambda req, timeout=10: FakeResponse(body),
    )
    ok, err, hit = app_module.geocode_address("empire state building")
    assert ok and err is None
    assert hit == {"lat": 40.748441, "lon": -73.985664,
                   "display_name": "Empire State Building, New York"}


def test_geocode_address_no_match_and_blank_query(monkeypatch):
    from ee_gateway_ui import app as app_module

    ok, err, hit = app_module.geocode_address("   ")
    assert not ok and hit is None

    class FakeResponse:
        def read(self):
            return b"[]"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        app_module.urllib.request, "urlopen",
        lambda req, timeout=10: FakeResponse(),
    )
    ok, err, hit = app_module.geocode_address("nowhere at all")
    assert not ok and hit is None
    assert "No match" in err


def test_settings_lookup_failure_preserves_saved_coordinates(client, tmp_path, monkeypatch):
    """A failed lookup on the settings page re-renders with the currently
    saved location still in the coordinate fields (the standalone lookup
    form does not carry them), so the operator's working config is never
    visually replaced by an error state."""
    _write_full_config(tmp_path)
    _stub_geocode(monkeypatch, result=(False, "No match for that address.", None))
    response = client.post(
        "/settings/location",
        data={"action": "lookup", "address": "asdfghjkl"},
    )
    assert response.status_code == 400
    assert b"40.712082" in response.data
    assert b"-74.0409" in response.data


def test_location_pair_out_of_range_gets_accurate_range_message(client, tmp_path):
    """An out-of-range pair paste reports the range error, not a
    misleading 'must be decimal degrees' / 'both required' error."""
    response = client.post(
        "/settings/location",
        data={"fixed_lat": "95.0, 10.0", "fixed_lon": ""},
    )
    assert response.status_code == 400
    assert b"between -90 and 90" in response.data
    assert not (tmp_path / "config.json").exists()


def test_version_metadata_in_sync():
    """pyproject.toml and __init__.py __version__ must agree. This drift
    shipped three releases in a row (0.7.4 pyproject under a 0.7.6
    worker) before 0.10.6 re-synced them; this test makes the next
    drift a test failure instead of a release-notes archaeology item."""
    import tomllib
    from pathlib import Path
    import ee_gateway_ui

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as fh:
        declared = tomllib.load(fh)["project"]["version"]
    assert declared == ee_gateway_ui.__version__
