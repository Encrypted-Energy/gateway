# EE Gateway worker — tests for packet flatten / rebuild.
# Copyright (C) 2026 encryptedenergy.com
# Licensed under the GNU General Public License version 3 (GPL-3.0-only).
# See the LICENSE file at the repository root.

"""Tests for the pure functions in ee_gateway_worker.main.

The thread loops need real BLE hardware and are verified on-device.
``_flatten`` and ``_rebuild_for_ee`` are pure and carry the Option A
logic (turning an SDK packet object into a storable row and back), so
they are unit-tested here.
"""

import base64
import json

from hubblenetwork.packets import (
    AesEaxPacket,
    EncryptedPacket,
    Location,
    UnencryptedPacket,
)

from ee_gateway_worker import main
from ee_gateway_worker.gps import GpsFix

_LOC = Location(lat=90, lon=0, fake=True)
_FIX = GpsFix(lat=40.7128, lon=-74.0060, at=1700000000.0)


def test_flatten_encrypted_packet_extracts_eid_as_hex():
    pkt = EncryptedPacket(
        timestamp=1700000000,
        location=_LOC,
        payload=b"\x01\x02\x03",
        rssi=-42,
        protocol_version=0,
        eid=0xCE38F7D1,
    )
    row = main._flatten(pkt)
    assert row["packet_type"] == "EncryptedPacket"
    assert row["eid"] == "ce38f7d1"   # int -> lowercase hex, no 0x
    assert row["rssi"] == -42
    fields = json.loads(row["raw"])
    assert base64.b64decode(fields["payload_b64"]) == b"\x01\x02\x03"
    assert fields["timestamp"] == 1700000000


def test_flatten_encrypted_packet_without_eid():
    # AES-CTR EncryptedPacket can have eid=None.
    pkt = EncryptedPacket(
        timestamp=1700000000, location=_LOC, payload=b"x", rssi=-30, eid=None
    )
    row = main._flatten(pkt)
    assert row["eid"] is None
    assert row["packet_type"] == "EncryptedPacket"


def test_flatten_unencrypted_packet_has_no_eid():
    # UnencryptedPacket has network_id, not eid.
    pkt = UnencryptedPacket(
        timestamp=1700000000,
        location=_LOC,
        network_id=4378792717,
        protocol_version=1,
        payload=b"hello",
        rssi=-55,
    )
    row = main._flatten(pkt)
    assert row["eid"] is None
    assert row["packet_type"] == "UnencryptedPacket"
    assert row["rssi"] == -55


def test_flatten_aes_eax_packet_extracts_eid():
    pkt = AesEaxPacket(
        timestamp=1700000000,
        location=_LOC,
        protocol_version=2,
        nonce_salt=b"\x00\x01",
        eid=12345,
        payload=b"ab",
        auth_tag=b"\x00\x01\x02\x03",
        rssi=-60,
    )
    row = main._flatten(pkt)
    assert row["eid"] == format(12345, "x")
    assert row["packet_type"] == "AesEaxPacket"


def test_rebuild_for_ee_roundtrips_payload_rssi_and_gps_fix():
    original = EncryptedPacket(
        timestamp=1700000000,
        location=_LOC,
        payload=b"\xde\xad\xbe\xef",
        rssi=-37,
        eid=0xABCD,
    )
    row = main._flatten(original, fix=_FIX)
    rebuilt = main._rebuild_for_ee(row["raw"])
    assert rebuilt is not None
    assert base64.b64decode(rebuilt["payload_b64"]) == b"\xde\xad\xbe\xef"
    assert rebuilt["rssi"] == -37
    assert rebuilt["timestamp"] == 1700000000
    # GPS fix from scan time must round-trip exactly; the ingest endpoint
    # forwards these to Hubble.
    assert rebuilt["latitude"] == _FIX.lat
    assert rebuilt["longitude"] == _FIX.lon
    # Fix capture time rides along (0.8.0) so a packet uploaded later still
    # reports when its coordinate was actually captured.
    assert rebuilt["position_at"] == int(_FIX.at)


def test_rebuild_for_ee_passes_accuracy_through():
    fix = GpsFix(lat=40.7128, lon=-74.0060, at=1700000000.0, accuracy_m=6.5)
    pkt = EncryptedPacket(
        timestamp=1700000000, location=_LOC, payload=b"\x01", rssi=-40, eid=0xABCD
    )
    rebuilt = main._rebuild_for_ee(main._flatten(pkt, fix=fix)["raw"])
    assert rebuilt is not None
    assert rebuilt["accuracy_m"] == 6.5
    assert rebuilt["position_at"] == 1700000000


def test_rebuild_for_ee_legacy_row_has_no_position_at():
    # Rows stored by a pre-0.8.0 worker carry coords but no fix metadata;
    # both keys must come back None so wire_packet omits them from the body.
    legacy_raw = json.dumps(
        {"payload_b64": "AA==", "rssi": -50, "timestamp": 1700000000,
         "latitude": 40.7, "longitude": -74.0, "eid": "abcd"}
    )
    rebuilt = main._rebuild_for_ee(legacy_raw)
    assert rebuilt is not None
    assert rebuilt["position_at"] is None
    assert rebuilt["accuracy_m"] is None


def test_rebuild_for_ee_handles_empty_payload():
    pkt = EncryptedPacket(timestamp=1700000000, location=_LOC, payload=b"", rssi=-50)
    rebuilt = main._rebuild_for_ee(main._flatten(pkt, fix=_FIX)["raw"])
    assert rebuilt is not None
    assert rebuilt["payload_b64"] == ""


def test_rebuild_for_ee_returns_none_when_no_gps_fix_stored():
    # 0.4.0 worker (or 0.5.0 pre-first-fix) stores a row without lat/lon.
    # _rebuild_for_ee returns None so the caller can skip it instead of
    # forwarding a coordinate-less packet to a Hubble cloud that would
    # reject it anyway.
    pkt = EncryptedPacket(timestamp=1700000000, location=_LOC, payload=b"x", rssi=-30)
    row = main._flatten(pkt, fix=None)
    assert main._rebuild_for_ee(row["raw"]) is None


# --- restart signal (0.6.3+) -----------------------------------------------

def test_consume_restart_signal_returns_false_when_absent(tmp_path, monkeypatch):
    """No sentinel file -> _consume_restart_signal is a no-op false."""
    monkeypatch.setattr(main, "RESTART_SIGNAL_PATH", str(tmp_path / ".restart_requested"))
    assert main._consume_restart_signal() is False


def test_consume_restart_signal_returns_true_and_deletes(tmp_path, monkeypatch):
    """Sentinel present -> returns True and removes the file so the
    request is honored exactly once."""
    signal_path = tmp_path / ".restart_requested"
    signal_path.touch()
    monkeypatch.setattr(main, "RESTART_SIGNAL_PATH", str(signal_path))

    assert main._consume_restart_signal() is True
    assert not signal_path.exists()
    # Second call sees no file.
    assert main._consume_restart_signal() is False


# --- gps.json for the UI (0.8.1+) ------------------------------------------

def test_write_gps_json_with_raw_fix(tmp_path):
    """A live dongle fix lands in gps.json with coords and timestamps."""
    import time as _time

    class _Client:
        def raw_fix(self):
            return GpsFix(lat=48.8971, lon=2.3467, at=1700000000.0)

    target = tmp_path / "gps.json"
    main._write_gps_json(_Client(), path=str(target), now=1700000010.0)

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {
        "updated_at": 1700000010.0,
        "lat": 48.8971,
        "lon": 2.3467,
        "at": 1700000000.0,
    }


def test_write_gps_json_without_fix_still_writes(tmp_path):
    """No fix -> the file still exists with only updated_at, so the UI can
    tell 'no fix right now' from 'worker too old to publish gps.json'."""

    class _Client:
        def raw_fix(self):
            return None

    target = tmp_path / "gps.json"
    main._write_gps_json(_Client(), path=str(target), now=1700000010.0)

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data == {"updated_at": 1700000010.0}


def test_raw_fix_ignores_fixed_location_override():
    """The 'use GPS position' shortcut must offer the DONGLE's position,
    never echo the already-saved override back through gps.json."""
    import time as _time
    from ee_gateway_worker import gps as gps_mod

    client = gps_mod.GpsClient(fixed_lat=1.0, fixed_lon=2.0)  # not started
    assert client.raw_fix() is None  # override alone is not a raw fix

    with client._lock:
        client._fix = GpsFix(lat=3.0, lon=4.0, at=_time.time())
    raw = client.raw_fix()
    assert (raw.lat, raw.lon) == (3.0, 4.0)
    # current_fix still prefers the override for the packet path.
    current = client.current_fix()
    assert (current.lat, current.lon) == (1.0, 2.0)
