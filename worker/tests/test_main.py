# EE Gateway worker — tests for packet flatten / rebuild.
# Copyright (C) 2026 encryptedenergy.com
# Licensed under the GNU General Public License version 3 (GPL-3.0-only).
# See the LICENSE file at the repository root.

"""Tests for the pure functions in ee_gateway_worker.main.

The thread loops need real BLE hardware and are verified on-device. ``_flatten``
and ``_rebuild_encrypted`` are pure and carry the Option A logic (turning an
SDK packet object into a storable row and back), so they are unit-tested here.
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

_LOC = Location(lat=90, lon=0, fake=True)


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


def test_rebuild_encrypted_roundtrips_payload_and_rssi():
    original = EncryptedPacket(
        timestamp=1700000000,
        location=_LOC,
        payload=b"\xde\xad\xbe\xef",
        rssi=-37,
        eid=0xABCD,
    )
    row = main._flatten(original)
    rebuilt = main._rebuild_encrypted(row["raw"])
    assert isinstance(rebuilt, EncryptedPacket)
    assert rebuilt.payload == b"\xde\xad\xbe\xef"
    assert rebuilt.rssi == -37
    assert rebuilt.timestamp == 1700000000
    # ingest_packet reads location.lat / location.lon — must be present.
    assert rebuilt.location is not None


def test_rebuild_encrypted_handles_empty_payload():
    pkt = EncryptedPacket(timestamp=1700000000, location=_LOC, payload=b"", rssi=-50)
    rebuilt = main._rebuild_encrypted(main._flatten(pkt)["raw"])
    assert rebuilt.payload == b""
