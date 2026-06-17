# EE Gateway UI — Flask application.
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

"""Setup wizard and read-only dashboard for the EE Gateway.

This is the unprivileged half of the gateway. It runs in its own container,
never touches Bluetooth, and never imports the Hubble SDK or the worker
package. It shares one directory with the worker and communicates entirely
through three files in that directory:

* ``config.json`` — the UI writes it (the setup form); the worker reads it.
* ``state.json``  — the worker writes it; the UI reads it for status counts.
* ``packets.db``  — the worker writes it; the UI opens it strictly read-only.

The ``packet_log`` schema is a contract shared with ``worker/db.py``. Because
the two containers are deliberately independent, this file re-declares the
handful of columns it reads rather than importing them. If the worker's
schema changes, the device query below must change with it.

Every read degrades gracefully: a missing or half-written file is an expected
state (the worker may not have started yet), not an error, so the dashboard
always renders something sensible.
"""

import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, redirect, render_template, request, url_for

DEFAULT_DATA_DIR = "/data"
DEFAULT_PORT = 8080

# After a credentials save, the UI presents an optimistic "Verifying
# credentials" state for this many seconds even if the worker's state.json
# still says "needs_setup". The worker re-reads config.json on its next scan
# cycle (~15s) and refreshes state.json on the cycle after that, so 60s
# comfortably covers the round-trip. After this window, the dashboard
# shows the worker's real status (catching truly-bad credentials).
VERIFYING_WINDOW_SECONDS = 60

# status code in state.json -> (display label, tone class used by the CSS)
_STATUS_LABELS = {
    "starting": ("Starting up", "neutral"),
    "running": ("Running", "ok"),
    "needs_setup": ("Waiting for credentials", "warn"),
    "scan_error": ("Bluetooth scan error", "error"),
    "auth_error": ("Authentication failed", "error"),
    "stopped": ("Stopped", "neutral"),
}

# Per-device rollup. Mirrors worker/db.py's device_summary(): newest packet's
# RSSI via a correlated subquery, NULL eids excluded, most-recent device first.
_DEVICE_QUERY = """
    SELECT eid,
           COUNT(*)        AS packets,
           MIN(scanned_at) AS first_seen,
           MAX(scanned_at) AS last_seen,
           (SELECT rssi FROM packet_log inner_log
             WHERE inner_log.eid = packet_log.eid
             ORDER BY id DESC LIMIT 1) AS last_rssi
      FROM packet_log
     WHERE eid IS NOT NULL
     GROUP BY eid
     ORDER BY last_seen DESC
     LIMIT 100
"""


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def _config_path(data_dir):
    return os.path.join(data_dir, "config.json")


def _state_path(data_dir):
    return os.path.join(data_dir, "state.json")


def _db_path(data_dir):
    return os.path.join(data_dir, "packets.db")


# --------------------------------------------------------------------------
# Reads (all failure-tolerant)
# --------------------------------------------------------------------------

def _read_json(path):
    """Parse a JSON file; return ``None`` on any failure.

    A missing or half-written file is expected, not exceptional: the worker
    may not have started yet, or may be writing as we read. Callers treat a
    ``None`` result as 'not available yet'.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def read_config(data_dir):
    """Return stored Hubble credentials, or ``None`` if not configured.

    Only a config carrying a non-empty ``org_id`` and ``api_token`` counts as
    configured. A missing file, malformed JSON, or blank fields all read as
    'not configured', so the UI falls back to the setup wizard.
    """
    raw = _read_json(_config_path(data_dir))
    if not isinstance(raw, dict):
        return None
    org_id = str(raw.get("org_id") or "").strip()
    api_token = str(raw.get("api_token") or "").strip()
    if not org_id or not api_token:
        return None
    return {"org_id": org_id, "api_token": api_token}


def read_state(data_dir):
    """Return the worker's last-written state dict, or ``None`` if unavailable."""
    raw = _read_json(_state_path(data_dir))
    return raw if isinstance(raw, dict) else None


def read_devices(data_dir):
    """Return per-device rollup rows from the worker's packet database.

    The database is opened strictly read-only (``mode=ro``): a bug in the UI
    can never write to or corrupt the worker's store. Any failure — the file
    does not exist yet, the table is missing, the worker is mid-write —
    degrades to an empty list so the dashboard still renders.
    """
    path = _db_path(data_dir)
    if not os.path.exists(path):
        return []
    try:
        uri = Path(os.path.abspath(path)).as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except (sqlite3.Error, ValueError):
        return []
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_DEVICE_QUERY).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [
        {
            "eid": row["eid"],
            "packets": row["packets"],
            "first_seen": _format_epoch(row["first_seen"]),
            "last_seen": _format_epoch(row["last_seen"]),
            "last_rssi": row["last_rssi"],
        }
        for row in rows
    ]


# --------------------------------------------------------------------------
# Write
# --------------------------------------------------------------------------

def write_config(data_dir, org_id, api_token):
    """Update credentials in ``config.json`` atomically, preserving other keys.

    From 0.5.0 the UI writes additional fields to this file (fixed_lat /
    fixed_lon for the location override). Saving credentials must NOT
    clobber those, so we read-merge-write rather than overwrite. The
    worker supplies its own defaults for any key the file omits.

    The dashboard reads ``saved_at`` to show an optimistic "Verifying
    credentials" state for a short window after submission, masking the
    natural lag between this write and the worker's next scan cycle.
    """
    _merge_config(data_dir, {
        "org_id": org_id,
        "api_token": api_token,
        "saved_at": int(time.time()),
    })


def write_advanced_config(data_dir, fixed_lat, fixed_lon):
    """Update fixed-location override in ``config.json``, preserving credentials.

    Both ``fixed_lat`` and ``fixed_lon`` must be present together for the
    override to be persisted; either both ``None`` or both floats. Passing
    ``None`` for both clears any existing override (back to gpsd).
    """
    existing = _read_json(_config_path(data_dir)) or {}
    if not isinstance(existing, dict):
        existing = {}
    updates: dict = {"saved_at": int(time.time())}
    if fixed_lat is None or fixed_lon is None:
        # Clear: drop the keys entirely so the worker's "no override" path is
        # unambiguous. Falsy-but-present zeros would be a valid coord at
        # (0, 0) and shouldn't be confused with "cleared."
        existing.pop("fixed_lat", None)
        existing.pop("fixed_lon", None)
    else:
        updates["fixed_lat"] = float(fixed_lat)
        updates["fixed_lon"] = float(fixed_lon)
    _merge_config(data_dir, updates, existing=existing)


def _merge_config(data_dir, updates, existing=None):
    """Atomic read-modify-write of ``config.json``.

    Reads the current file (or treats it as ``{}`` if missing or malformed),
    overlays ``updates``, and writes the result via temp-file + ``os.replace``
    so the worker, which re-reads this file every cycle, never sees a
    partial config. Pass ``existing`` to avoid a second disk read when the
    caller already loaded it.
    """
    path = _config_path(data_dir)
    if existing is None:
        existing = _read_json(path) or {}
    if not isinstance(existing, dict):
        existing = {}
    existing.update(updates)
    payload = json.dumps(existing, indent=2)
    directory = os.path.dirname(path) or "."
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory,
        prefix=".config-", suffix=".tmp", delete=False,
    )
    try:
        with handle:
            handle.write(payload + "\n")
        os.replace(handle.name, path)
    except OSError:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def read_advanced_config(data_dir):
    """Return current fixed-location override values, or ``(None, None)``."""
    raw = _read_json(_config_path(data_dir))
    if not isinstance(raw, dict):
        return None, None
    lat = raw.get("fixed_lat")
    lon = raw.get("fixed_lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None, None
    return float(lat), float(lon)


# --------------------------------------------------------------------------
# View helpers
# --------------------------------------------------------------------------

def _format_epoch(value):
    """Render an epoch-seconds value as a local timestamp, or a dash."""
    if value is None:
        return "-"
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def _status_view(state):
    """Translate the worker's state file into display fields for the badge."""
    if not state:
        return {
            "code": "unknown",
            "label": "Worker not reporting yet",
            "tone": "neutral",
            "error": None,
            "needs_creds": False,
        }
    code = str(state.get("status") or "unknown")
    label, tone = _STATUS_LABELS.get(
        code, (code.replace("_", " ").capitalize(), "neutral")
    )
    return {
        "code": code,
        "label": label,
        "tone": tone,
        "error": state.get("error"),
        "needs_creds": code == "auth_error",
    }


def _apply_verifying_override(status, raw_config, now=None):
    """If credentials were just saved and the worker hasn't picked them up yet,
    swap the status badge to an optimistic "Verifying credentials" state.

    Closes the UX gap where, for the ~15-30s between config.json being
    written and the worker reading it on its next scan cycle, the
    dashboard would otherwise read "Waiting for credentials" — implying
    the operator typed something wrong.

    Returns the (possibly overridden) status dict. The override only
    applies when the worker's current status is "needs_setup" or
    "unknown": once the worker is actually running, its real state
    wins immediately.
    """
    if not isinstance(raw_config, dict):
        return status
    saved_at = raw_config.get("saved_at")
    if not isinstance(saved_at, (int, float)):
        return status
    elapsed = (now if now is not None else time.time()) - saved_at
    if elapsed < 0 or elapsed >= VERIFYING_WINDOW_SECONDS:
        return status
    if status["code"] not in ("needs_setup", "unknown"):
        return status
    return {
        "code": "verifying",
        "label": "Verifying credentials",
        "tone": "neutral",
        "error": None,
        "needs_creds": False,
    }


def _counts_view(state):
    """Pull the four headline counters out of the worker's state file."""
    packets = (state or {}).get("packets") or {}

    def _count(key):
        value = packets.get(key)
        return value if isinstance(value, int) else None

    return {
        "total": _count("total"),
        "ingested": _count("ingested"),
        "pending": _count("pending"),
        "devices": _count("devices"),
    }


# --------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------

def create_app(data_dir=None):
    """Build the Flask application.

    ``data_dir`` is the directory shared with the worker container. When not
    given it falls back to the ``EE_DATA_DIR`` environment variable, then to
    ``/data`` — the path baked into the container image. Tests inject a
    temporary directory directly, which is why this is a parameter rather
    than read at import time.
    """
    if data_dir is None:
        data_dir = os.environ.get("EE_DATA_DIR", DEFAULT_DATA_DIR)
    data_dir = str(data_dir)

    app = Flask(__name__)
    app.config["EE_DATA_DIR"] = data_dir

    # Inject the gateway version into every template so the brand line in
    # base.html can show what's actually running. Sourced from the
    # EE_GATEWAY_VERSION env var (passed by the umbrel compose); falls back
    # to "" (which the template then renders as nothing) when unset, so
    # local dev runs don't show a stale or wrong number.
    gateway_version = os.environ.get("EE_GATEWAY_VERSION", "").strip()

    @app.context_processor
    def inject_version():
        return {"gateway_version": gateway_version}

    @app.route("/healthz")
    def healthz():
        """Liveness probe for the container / app proxy."""
        return "ok", 200

    @app.route("/")
    def index():
        """Setup wizard when unconfigured, dashboard once credentials exist."""
        config = read_config(data_dir)
        if config is None:
            return render_template(
                "setup.html", org_id="", error=None, configured=False
            )
        state = read_state(data_dir)
        raw_config = _read_json(_config_path(data_dir))
        status = _apply_verifying_override(_status_view(state), raw_config)
        return render_template(
            "dashboard.html",
            status=status,
            counts=_counts_view(state),
            devices=read_devices(data_dir),
            updated=_format_epoch(state.get("updated_at") if state else None),
        )

    @app.route("/setup")
    def setup():
        """Show the credentials form, even when already configured.

        The organization ID is pre-filled when a config exists; the API token
        field is always left blank and never echoed back — the stored secret
        is write-only from the UI's point of view.
        """
        config = read_config(data_dir)
        return render_template(
            "setup.html",
            org_id=config["org_id"] if config else "",
            error=None,
            configured=config is not None,
        )

    @app.route("/restart", methods=["POST"])
    def restart_worker():
        """Ask the worker to restart by touching a sentinel file.

        The UI cannot signal the worker container directly (separate
        process / cgroup / network namespace), but they share the data
        directory. Worker 0.6.3+ polls for this file once per scan cycle;
        when present, it deletes the file and exits non-zero. Docker's
        `restart: on-failure` policy on the worker service brings a
        fresh container back up within seconds.

        Failures to touch the file (read-only filesystem, etc.) are
        logged silently. The caller is redirected back to / either way,
        so the user sees the dashboard refresh.
        """
        sentinel = os.path.join(data_dir, ".restart_requested")
        try:
            Path(sentinel).touch()
        except OSError as exc:
            app.logger.warning("could not touch restart sentinel: %s", exc)
        return redirect(url_for("index"))

    @app.route("/config", methods=["POST"])
    def save_config():
        """Validate and persist submitted credentials, then return to ``/``."""
        org_id = (request.form.get("org_id") or "").strip()
        api_token = (request.form.get("api_token") or "").strip()
        if not org_id or not api_token:
            return (
                render_template(
                    "setup.html",
                    org_id=org_id,
                    error="Both the organization ID and API token are required.",
                    configured=read_config(data_dir) is not None,
                ),
                400,
            )
        write_config(data_dir, org_id, api_token)
        return redirect(url_for("index"))

    @app.route("/advanced", methods=["GET"])
    def advanced():
        """Show the fixed-location override form."""
        lat, lon = read_advanced_config(data_dir)
        return render_template(
            "advanced.html",
            fixed_lat="" if lat is None else lat,
            fixed_lon="" if lon is None else lon,
            error=None,
            saved=False,
        )

    @app.route("/advanced", methods=["POST"])
    def save_advanced():
        """Validate and persist the fixed-location override.

        Both fields empty -> clears the override. Either both must be
        non-empty floats in range, or both must be empty. One alone is
        treated as a validation error (and re-renders the form) so an
        operator who started filling the form out can't accidentally
        save a half-configured override.

        On a successful save, the worker is asked to restart so the new
        GpsClient picks up the change on next boot (gps mode is set at
        worker boot; restart is the cleanest way to apply it).
        """
        raw_lat = (request.form.get("fixed_lat") or "").strip()
        raw_lon = (request.form.get("fixed_lon") or "").strip()

        if not raw_lat and not raw_lon:
            write_advanced_config(data_dir, None, None)
            _touch_restart_sentinel(data_dir)
            return render_template(
                "advanced.html",
                fixed_lat="",
                fixed_lon="",
                error=None,
                saved=True,
            )

        if not raw_lat or not raw_lon:
            return (
                render_template(
                    "advanced.html",
                    fixed_lat=raw_lat,
                    fixed_lon=raw_lon,
                    error="Both latitude and longitude are required, or clear both to disable.",
                    saved=False,
                ),
                400,
            )

        try:
            lat = float(raw_lat)
            lon = float(raw_lon)
        except ValueError:
            return (
                render_template(
                    "advanced.html",
                    fixed_lat=raw_lat,
                    fixed_lon=raw_lon,
                    error="Latitude and longitude must be numeric (e.g. 40.712082, -74.040900).",
                    saved=False,
                ),
                400,
            )

        if not -90.0 <= lat <= 90.0:
            return (
                render_template(
                    "advanced.html",
                    fixed_lat=raw_lat,
                    fixed_lon=raw_lon,
                    error="Latitude must be between -90 and 90.",
                    saved=False,
                ),
                400,
            )
        if not -180.0 <= lon <= 180.0:
            return (
                render_template(
                    "advanced.html",
                    fixed_lat=raw_lat,
                    fixed_lon=raw_lon,
                    error="Longitude must be between -180 and 180.",
                    saved=False,
                ),
                400,
            )

        write_advanced_config(data_dir, lat, lon)
        _touch_restart_sentinel(data_dir)
        return render_template(
            "advanced.html",
            fixed_lat=lat,
            fixed_lon=lon,
            error=None,
            saved=True,
        )

    return app


def _touch_restart_sentinel(data_dir):
    """Ask the worker to restart so a config change takes effect on its
    next boot. Quiet failure: the operator can still hit "Restart worker"
    manually if this somehow couldn't write."""
    try:
        Path(os.path.join(data_dir, ".restart_requested")).touch()
    except OSError:
        pass


def main():
    """Run the development server (``python -m ee_gateway_ui.app``).

    Flask's built-in server is adequate for this single-user, self-hosted
    dashboard sitting behind Umbrel's app proxy. Moving to a production WSGI
    server (gunicorn) is a v1 consideration, not a v0 requirement.
    """
    port = int(os.environ.get("EE_UI_PORT", DEFAULT_PORT))
    create_app().run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
