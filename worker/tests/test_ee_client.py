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
import json
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


# ----------------------------------------------------------------------
# Batch upload (ingest_packets)
# ----------------------------------------------------------------------

class _OKResponse:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b""


def _capture_urlopen(captured):
    """urlopen replacement that records the request body and returns 200."""

    def _fake(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["url"] = request.full_url
        return _OKResponse()

    return _fake


def _sample_packets(n):
    return [
        ee_client.wire_packet(
            payload_b64="AA==", rssi=-50, timestamp=i, latitude=40.7, longitude=-74.0, eid=f"{i:04x}"
        )
        for i in range(n)
    ]


def test_ingest_packets_sends_whole_batch_in_one_request(monkeypatch):
    captured = {}
    monkeypatch.setattr(ee_client.urllib.request, "urlopen", _capture_urlopen(captured))

    ee_client.ingest_packets(
        base_url="https://encryptedenergy.com",
        api_token="ee_live_test",
        packets=_sample_packets(3),
    )

    assert captured["url"].endswith("/api/v1/gateways/packets")
    assert len(captured["body"]["packets"]) == 3


def test_ingest_packets_empty_is_a_noop(monkeypatch):
    def _must_not_call(request, timeout=None):
        raise AssertionError("empty batch should not hit the network")

    monkeypatch.setattr(ee_client.urllib.request, "urlopen", _must_not_call)
    assert ee_client.ingest_packets(
        base_url="https://encryptedenergy.com", api_token="ee_live_test", packets=[]
    ) is None


def test_ingest_packets_classifies_retryable_batch_response(monkeypatch):
    monkeypatch.setattr(ee_client.urllib.request, "urlopen", _raise_http(429))
    with pytest.raises(IngestTransient):
        ee_client.ingest_packets(
            base_url="https://encryptedenergy.com",
            api_token="ee_live_test",
            packets=_sample_packets(5),
        )


def test_wire_packet_omits_eid_when_none():
    p = ee_client.wire_packet(payload_b64="AA==", rssi=-50, timestamp=1, latitude=1.0, longitude=2.0)
    assert "eid" not in p
    p2 = ee_client.wire_packet(payload_b64="AA==", rssi=-50, timestamp=1, latitude=1.0, longitude=2.0, eid="ab")
    assert p2["eid"] == "ab"
