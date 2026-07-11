# EE Gateway worker — tests for the EE ingest client's failure classification.
# Copyright (C) 2026 encryptedenergy.com
# Licensed under the GNU General Public License version 3 (GPL-3.0-only).
# See the LICENSE file at the repository root.

"""Tests for ee_gateway_worker.ee_client HTTP status classification.

These guard the "gateway 6" data-loss regression: a retryable response
(EE-side credential failure surfaced as 502, or a 408/429 back-off) must
leave the packet PENDING (IngestTransient), never be dropped as terminal.
Only a genuine malformed-packet 4xx is terminal; a 401 is unauthorized.
"""

import io
import urllib.error

import pytest

from ee_gateway_worker import ee_client
from ee_gateway_worker.ee_client import (
    IngestTerminal,
    IngestTransient,
    IngestUnauthorized,
)


def _raise_http(code):
    """Return a urlopen replacement that raises HTTPError with ``code``."""

    def _fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            url=request.full_url,
            code=code,
            msg=f"status {code}",
            hdrs=None,
            fp=io.BytesIO(b""),
        )

    return _fake_urlopen


def _ingest():
    ee_client.ingest_packet(
        base_url="https://encryptedenergy.com",
        api_token="ee_live_test",
        payload_b64="AA==",
        rssi=-50,
        timestamp=1,
        latitude=40.7,
        longitude=-74.0,
        eid="abcd",
    )


@pytest.mark.parametrize("code", [500, 502, 503, 504, 408, 429])
def test_retryable_statuses_are_transient(monkeypatch, code):
    monkeypatch.setattr(ee_client.urllib.request, "urlopen", _raise_http(code))
    with pytest.raises(IngestTransient):
        _ingest()


@pytest.mark.parametrize("code", [400, 403, 404, 422])
def test_terminal_statuses_are_terminal(monkeypatch, code):
    monkeypatch.setattr(ee_client.urllib.request, "urlopen", _raise_http(code))
    with pytest.raises(IngestTerminal) as exc:
        _ingest()
    # A terminal error must NOT be the Unauthorized subclass (which the loop
    # treats specially by flagging the dashboard).
    assert not isinstance(exc.value, IngestUnauthorized)


def test_401_is_unauthorized(monkeypatch):
    monkeypatch.setattr(ee_client.urllib.request, "urlopen", _raise_http(401))
    with pytest.raises(IngestUnauthorized):
        _ingest()


def test_network_failure_is_transient(monkeypatch):
    def _boom(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ee_client.urllib.request, "urlopen", _boom)
    with pytest.raises(IngestTransient):
        _ingest()
