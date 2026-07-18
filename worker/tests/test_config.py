# EE Gateway worker — tests for configuration loading.
# Copyright (C) 2026 encryptedenergy.com
# Licensed under the GNU General Public License version 3 (GPL-3.0-only).
# See the LICENSE file at the repository root.

"""Tests for ee_gateway_worker.config.

The ``clean_env`` fixture strips the EE/HUBBLE variables before every
test, so a variable set in the developer's shell cannot leak in and make a
test pass or fail by accident. Tests that exercise env precedence set them
explicitly via monkeypatch.
"""

import json

import pytest

from ee_gateway_worker import config
from ee_gateway_worker.config import Config, ConfigError

_ENV_VARS = (
    "EE_API_TOKEN",
    "HUBBLE_API_TOKEN",
    "EE_ORG_ID",        # legacy; stripped so a dev shell cannot leak it in
    "HUBBLE_ORG_ID",    # legacy
    "EE_SCAN_INTERVAL",
    "EE_SCAN_TIMEOUT",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --- credentials -----------------------------------------------------------

def test_load_from_file_only(tmp_path):
    # The file may still carry a legacy org_id key (pre-0.8.0 installs);
    # it is ignored, and Config no longer has the field at all.
    cfgfile = _write(tmp_path / "config.json", {"org_id": "org-1", "api_token": "tok-1"})
    cfg = config.load(cfgfile)
    assert cfg == Config(api_token="tok-1")
    assert not hasattr(cfg, "org_id")


def test_load_from_env_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBBLE_API_TOKEN", "tok-env")
    cfg = config.load(tmp_path / "does-not-exist.json")
    assert cfg.api_token == "tok-env"


def test_env_overrides_file(tmp_path, monkeypatch):
    cfgfile = _write(tmp_path / "config.json", {"api_token": "tok-file"})
    monkeypatch.setenv("EE_API_TOKEN", "tok-env")
    cfg = config.load(cfgfile)
    assert cfg.api_token == "tok-env"   # env wins


def test_empty_env_var_does_not_override_file(tmp_path, monkeypatch):
    cfgfile = _write(tmp_path / "config.json", {"api_token": "tok-file"})
    monkeypatch.setenv("EE_API_TOKEN", "")  # empty -> ignored, not an override
    cfg = config.load(cfgfile)
    assert cfg.api_token == "tok-file"


def test_missing_credentials_raises_about_token_only(tmp_path):
    # The api_token is the only credential; it is the only thing the
    # error names.
    with pytest.raises(ConfigError, match="api_token"):
        config.load(tmp_path / "absent.json")


def test_token_only_config_loads(tmp_path):
    """A 0.6.4+ UI writes token-only configs; this is the canonical shape."""
    cfgfile = _write(tmp_path / "config.json", {"api_token": "tok-1"})
    cfg = config.load(cfgfile)
    assert cfg.api_token == "tok-1"


def test_missing_token_raises_and_names_it(tmp_path):
    cfgfile = _write(tmp_path / "config.json", {"org_id": "org-1"})
    with pytest.raises(ConfigError, match="api_token"):
        config.load(cfgfile)


# --- timing values ---------------------------------------------------------

def test_timing_defaults_applied(tmp_path):
    cfgfile = _write(tmp_path / "config.json", {"org_id": "o", "api_token": "t"})
    cfg = config.load(cfgfile)
    assert cfg.scan_interval == config.DEFAULT_SCAN_INTERVAL
    assert cfg.scan_timeout == config.DEFAULT_SCAN_TIMEOUT


def test_timing_from_file(tmp_path):
    cfgfile = _write(
        tmp_path / "config.json",
        {"org_id": "o", "api_token": "t", "scan_interval": 30, "scan_timeout": 5},
    )
    cfg = config.load(cfgfile)
    assert cfg.scan_interval == 30
    assert cfg.scan_timeout == 5


def test_timing_env_overrides_file(tmp_path, monkeypatch):
    cfgfile = _write(
        tmp_path / "config.json",
        {"org_id": "o", "api_token": "t", "scan_interval": 30},
    )
    monkeypatch.setenv("EE_SCAN_INTERVAL", "45")
    assert config.load(cfgfile).scan_interval == 45


def test_non_integer_timing_raises(tmp_path):
    cfgfile = _write(
        tmp_path / "config.json",
        {"org_id": "o", "api_token": "t", "scan_interval": "soon"},
    )
    with pytest.raises(ConfigError, match="scan_interval"):
        config.load(cfgfile)


def test_out_of_range_timeout_raises(tmp_path):
    cfgfile = _write(
        tmp_path / "config.json",
        {"org_id": "o", "api_token": "t", "scan_timeout": 0},
    )
    with pytest.raises(ConfigError, match="scan_timeout"):
        config.load(cfgfile)


# --- malformed file --------------------------------------------------------

def test_malformed_json_falls_back_to_env(tmp_path, monkeypatch):
    bad = tmp_path / "config.json"
    bad.write_text('{"org_id": "o", "api_token":', encoding="utf-8")  # truncated
    monkeypatch.setenv("HUBBLE_API_TOKEN", "tok-env")
    cfg = config.load(bad)  # must not raise on the bad file
    assert cfg.api_token == "tok-env"


def test_malformed_json_without_env_raises_missing_creds(tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("not json at all", encoding="utf-8")
    with pytest.raises(ConfigError, match="missing required credential"):
        config.load(bad)


def test_json_array_instead_of_object_is_ignored(tmp_path, monkeypatch):
    weird = tmp_path / "config.json"
    weird.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setenv("HUBBLE_API_TOKEN", "t")
    assert config.load(weird).api_token == "t"


def test_version_metadata_in_sync():
    """pyproject.toml and __init__.py __version__ must agree. This drift
    shipped three releases in a row (pyproject 0.7.4 under a 0.7.6
    worker) before 0.10.6 re-synced them; this test makes the next
    drift a test failure instead of a release-notes archaeology item."""
    import tomllib
    from pathlib import Path

    import ee_gateway_worker

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with open(pyproject, "rb") as fh:
        declared = tomllib.load(fh)["project"]["version"]
    assert declared == ee_gateway_worker.__version__
