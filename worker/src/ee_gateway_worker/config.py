# EE Gateway worker — configuration loading and validation.
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

"""Gateway configuration.

A :class:`Config` carries everything the worker needs to run: the API
token and the scan-loop timings.

Values come from three sources, highest priority first:

1. environment variables (``EE_API_TOKEN``, ``EE_SCAN_INTERVAL``,
   ``EE_SCAN_TIMEOUT``, plus legacy ``HUBBLE_*`` aliases);
2. the JSON file the UI container writes (``config.json``);
3. built-in defaults.

The API token is the one required credential — it must come from the env
or the file, or loading fails. ``org_id`` is a legacy field (pre-0.7.7):
still read from env/file into :class:`Config` for back-compat, never
required, never consumed. The two timing values have defaults and a
range check.

The worker reloads this configuration every scan cycle, so a save from the UI
takes effect within one cycle. A malformed or half-written file (the UI caught
mid-save) is treated as absent — the worker falls back to env vars and defaults
rather than crashing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

# Scan-loop defaults (seconds). SCAN_INTERVAL is the pause between scan cycles;
# SCAN_TIMEOUT is how long a single ble.scan() call listens.
DEFAULT_SCAN_INTERVAL = 15
DEFAULT_SCAN_TIMEOUT = 10

# Heartbeat defaults. The worker POSTs to encryptedenergy.com periodically so
# the EE dashboard shows the gateway as alive. Both are overridable via env
# (EE_BASE_URL, EE_HEARTBEAT_INTERVAL) or the file (ee_base_url, heartbeat_interval).
DEFAULT_EE_BASE_URL = "https://encryptedenergy.com"
DEFAULT_HEARTBEAT_INTERVAL = 60

# Accepted ranges. Lower bounds keep the radio from being hammered; upper
# bounds keep the gateway responsive to config changes and shutdown.
_MIN_INTERVAL, _MAX_INTERVAL = 0, 3600
_MIN_TIMEOUT, _MAX_TIMEOUT = 1, 300
_MIN_HEARTBEAT, _MAX_HEARTBEAT = 15, 3600


class ConfigError(Exception):
    """Raised when the resolved configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """A validated, immutable gateway configuration."""

    api_token: str
    # Legacy field (pre-0.7.7). Nothing consumes it — auth is the ee_live
    # token alone — but installs configured before the org-ID field was
    # removed from the UI still carry it in config.json, so it is read
    # and kept for forward-compat rather than treated as an error.
    org_id: str = ""
    scan_interval: int = DEFAULT_SCAN_INTERVAL
    scan_timeout: int = DEFAULT_SCAN_TIMEOUT
    ee_base_url: str = DEFAULT_EE_BASE_URL
    heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL
    # Fixed-location override (0.7.4+, set via UI -> /data/config.json).
    # Both must be present and in range for the override to activate.
    # Lives in config.json (persistent across Umbrel app updates) rather
    # than env vars (wiped on each compose refresh).
    fixed_lat: float | None = None
    fixed_lon: float | None = None


def _read_json_file(path: Path) -> dict:
    """Return the parsed config file, or ``{}`` if it is absent or unreadable.

    A missing file is normal before first-time setup. A malformed file is
    most likely the UI caught mid-write; either way the safe response is to
    contribute nothing and let env vars and defaults stand.
    """
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_int(value, field: str) -> int:
    """Convert a config value to int, raising :class:`ConfigError` if it cannot."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{field} must be an integer, got {value!r}")


def load(config_path: str | Path) -> Config:
    """Resolve, validate, and return the gateway configuration.

    Precedence is env var, then ``config.json``, then default. Raises
    :class:`ConfigError` if credentials are missing or a timing value is
    non-integer or out of range.
    """
    file_data = _read_json_file(Path(config_path))

    def pick(env_key: str, file_key: str, default=None):
        if env_key in os.environ and os.environ[env_key] != "":
            return os.environ[env_key]
        if file_data.get(file_key) not in (None, ""):
            return file_data[file_key]
        return default

    # EE_* are the canonical env var names from 0.4.0 onward. HUBBLE_* still
    # work for back-compat with installs created before the pivot. Env value
    # wins over file value; either canonical or legacy env name is accepted.
    org_id = (
        os.environ.get("EE_ORG_ID")
        or os.environ.get("HUBBLE_ORG_ID")
        or file_data.get("org_id")
    )
    api_token = (
        os.environ.get("EE_API_TOKEN")
        or os.environ.get("HUBBLE_API_TOKEN")
        or file_data.get("api_token")
    )
    # Only the API token is required (0.7.7+). org_id used to be required
    # here, but it was never consumed anywhere — the ee_live token alone
    # authenticates against ee-web — and the UI no longer collects it.
    if not api_token:
        raise ConfigError("missing required credential(s): api_token (EE_API_TOKEN)")

    scan_interval = _coerce_int(
        pick("EE_SCAN_INTERVAL", "scan_interval", DEFAULT_SCAN_INTERVAL),
        "scan_interval",
    )
    scan_timeout = _coerce_int(
        pick("EE_SCAN_TIMEOUT", "scan_timeout", DEFAULT_SCAN_TIMEOUT),
        "scan_timeout",
    )
    if not _MIN_INTERVAL <= scan_interval <= _MAX_INTERVAL:
        raise ConfigError(
            f"scan_interval must be {_MIN_INTERVAL}-{_MAX_INTERVAL}s, "
            f"got {scan_interval}"
        )
    if not _MIN_TIMEOUT <= scan_timeout <= _MAX_TIMEOUT:
        raise ConfigError(
            f"scan_timeout must be {_MIN_TIMEOUT}-{_MAX_TIMEOUT}s, "
            f"got {scan_timeout}"
        )

    ee_base_url = str(pick("EE_BASE_URL", "ee_base_url", DEFAULT_EE_BASE_URL))
    heartbeat_interval = _coerce_int(
        pick("EE_HEARTBEAT_INTERVAL", "heartbeat_interval", DEFAULT_HEARTBEAT_INTERVAL),
        "heartbeat_interval",
    )
    if not _MIN_HEARTBEAT <= heartbeat_interval <= _MAX_HEARTBEAT:
        raise ConfigError(
            f"heartbeat_interval must be {_MIN_HEARTBEAT}-{_MAX_HEARTBEAT}s, "
            f"got {heartbeat_interval}"
        )

    # Fixed-location override. Env wins (legacy 0.7.1+ behavior); file is
    # the persistent path (0.7.4+, set via UI /advanced). Either must
    # supply BOTH lat AND lon for the override to activate. Out-of-range
    # values are treated as unset (a typo in the UI can't crash the worker)
    # — the UI does its own range validation before saving, so this is a
    # defense-in-depth check.
    fixed_lat = _parse_coord(
        pick("EE_GPS_FIXED_LAT", "fixed_lat"),
        bounds=(-90.0, 90.0),
        field="fixed_lat",
    )
    fixed_lon = _parse_coord(
        pick("EE_GPS_FIXED_LON", "fixed_lon"),
        bounds=(-180.0, 180.0),
        field="fixed_lon",
    )
    # Both required; one alone is invalid and silently disabled.
    if fixed_lat is None or fixed_lon is None:
        fixed_lat = None
        fixed_lon = None

    return Config(
        org_id=str(org_id or ""),
        api_token=str(api_token),
        scan_interval=scan_interval,
        scan_timeout=scan_timeout,
        ee_base_url=ee_base_url,
        heartbeat_interval=heartbeat_interval,
        fixed_lat=fixed_lat,
        fixed_lon=fixed_lon,
    )


def _parse_coord(value, *, bounds: tuple[float, float], field: str) -> float | None:
    """Return ``value`` as a float in ``bounds``, or ``None`` if it can't.

    Used for the fixed-location override. Unparseable or out-of-range
    inputs return ``None`` so a typo never blocks worker startup. The
    UI does its own range validation before write; this is belt-and-
    suspenders.
    """
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    lo, hi = bounds
    if not lo <= f <= hi:
        return None
    return f
