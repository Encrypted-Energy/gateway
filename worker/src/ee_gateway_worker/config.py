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

A :class:`Config` carries everything the worker needs to run: the Hubble
credentials and the scan-loop timings.

Values come from three sources, highest priority first:

1. environment variables (``HUBBLE_ORG_ID``, ``HUBBLE_API_TOKEN``,
   ``EE_SCAN_INTERVAL``, ``EE_SCAN_TIMEOUT``);
2. the JSON file the UI container writes (``config.json``);
3. built-in defaults.

Credentials have no default — they must come from the env or the file, or
loading fails. The two timing values have defaults and a range check.

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

# Accepted ranges. Lower bounds keep the radio from being hammered; upper
# bounds keep the gateway responsive to config changes and shutdown.
_MIN_INTERVAL, _MAX_INTERVAL = 0, 3600
_MIN_TIMEOUT, _MAX_TIMEOUT = 1, 300


class ConfigError(Exception):
    """Raised when the resolved configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """A validated, immutable gateway configuration."""

    org_id: str
    api_token: str
    scan_interval: int = DEFAULT_SCAN_INTERVAL
    scan_timeout: int = DEFAULT_SCAN_TIMEOUT


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

    org_id = pick("HUBBLE_ORG_ID", "org_id")
    api_token = pick("HUBBLE_API_TOKEN", "api_token")
    if not org_id or not api_token:
        missing = []
        if not org_id:
            missing.append("org_id (HUBBLE_ORG_ID)")
        if not api_token:
            missing.append("api_token (HUBBLE_API_TOKEN)")
        raise ConfigError("missing required credential(s): " + ", ".join(missing))

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

    return Config(
        org_id=str(org_id),
        api_token=str(api_token),
        scan_interval=scan_interval,
        scan_timeout=scan_timeout,
    )
