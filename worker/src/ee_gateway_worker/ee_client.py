# EE Gateway worker, EE ingest client.
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

"""POST scanned BLE packets to encryptedenergy.com.

The worker no longer talks to Hubble directly. The user pastes EE-issued
credentials (ee_org_..., ee_live_...) into the setup wizard; the worker
forwards each packet to ``${EE_BASE_URL}/api/v1/gateways/packets``, and EE
proxies the packet to Hubble using its master wholesale account.

This module exposes :func:`ingest_packets`, which the ``ingest_loop`` in
``main.py`` calls with a whole batch of packets (up to EE's per-request
cap), and :func:`ingest_packet`, a single-packet convenience wrapper. EE
forwards the batch to Hubble in one upstream call and dedupes resends by a
content-derived batchId, so re-sending a batch after a lost response is
safe. Implemented with ``urllib.request`` to keep the worker free of new
dependencies.

Failure modes returned to the caller (identical for a single packet or a
batch — EE accepts or rejects the whole request as one unit):

* :class:`IngestTransient`  the request never reached EE, EE returned 5xx, or
  EE returned a retryable 4xx (408 timeout, 429 rate limit). The caller should
  leave the packets pending and retry on the next pass.
* :class:`IngestTerminal`   EE returned a terminal 4xx (malformed body, genuine
  Hubble 400/404/422, etc). The caller should drop the packets to clear them.
* :class:`IngestUnauthorized` EE returned 401 (bad/revoked token).
* ``None``                  success.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

PACKETS_PATH = "/api/v1/gateways/packets"
REQUEST_TIMEOUT_SECONDS = 10

log = logging.getLogger("ee_gateway_worker.ee_client")


class IngestError(Exception):
    """Base for ee_client ingest failures."""


class IngestTransient(IngestError):
    """A retryable failure (network or 5xx). Leave the packet pending."""


class IngestTerminal(IngestError):
    """A non-retryable 4xx. Drop the packet from the queue."""


class IngestUnauthorized(IngestTerminal):
    """The token was rejected (HTTP 401). Drop the packet AND flag the worker
    as auth-failed so the dashboard surfaces the credential problem.

    Subclasses IngestTerminal so a single ``except IngestTerminal`` still
    catches it; callers that care about the auth distinction catch this
    subclass first.
    """


def wire_packet(
    *,
    payload_b64: str,
    rssi: int | None,
    timestamp: int | None,
    latitude: float = 90.0,
    longitude: float = 0.0,
    eid: str | None = None,
) -> dict:
    """Build the JSON dict for one packet as EE's ingest API expects it.

    Coordinates default to (90, 0) for parity with the Hubble SDK's
    placeholder. ``eid`` (0.7.3+) is the hex device identifier the worker
    extracted locally; EE uses it to enforce a per-(org, device, day) bounty
    cap. Omitted from the body when None so older EE versions that don't
    expect the field stay compatible (extra fields are accepted regardless).
    """
    packet = {
        "payload_b64": payload_b64,
        "rssi": rssi,
        "timestamp": timestamp,
        "latitude": latitude,
        "longitude": longitude,
    }
    if eid is not None:
        packet["eid"] = eid
    return packet


def ingest_packets(*, base_url: str, api_token: str, packets: list[dict]) -> None:
    """POST a batch of packets to EE in one request for forwarding to Hubble.

    ``packets`` is a list of dicts from :func:`wire_packet`. EE accepts or
    rejects the whole batch as a unit and dedupes an identical resend, so a
    retry after a lost response does not double-ingest. An empty list is a
    no-op. Raises the same ``Ingest*`` exceptions as the single-packet path.
    """
    if not packets:
        return None
    return _post(base_url=base_url, api_token=api_token, body={"packets": packets})


def ingest_packet(
    *,
    base_url: str,
    api_token: str,
    payload_b64: str,
    rssi: int | None,
    timestamp: int | None,
    latitude: float = 90.0,
    longitude: float = 0.0,
    eid: str | None = None,
) -> None:
    """POST a single packet to EE. Thin wrapper over :func:`ingest_packets`."""
    return ingest_packets(
        base_url=base_url,
        api_token=api_token,
        packets=[
            wire_packet(
                payload_b64=payload_b64,
                rssi=rssi,
                timestamp=timestamp,
                latitude=latitude,
                longitude=longitude,
                eid=eid,
            )
        ],
    )


def _post(*, base_url: str, api_token: str, body: dict) -> None:
    """POST a JSON body to EE's packets endpoint and classify the response."""
    url = base_url.rstrip("/") + PACKETS_PATH
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ee-gateway-worker/ingest",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            response.read()
            return None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise IngestUnauthorized(
                f"EE rejected upload (HTTP 401): token invalid or revoked"
            ) from exc
        # 408 (timeout) and 429 (rate limit) are retryable, not terminal.
        # Dropping them on the floor permanently destroys the packets the next
        # time EE throttles a busy gateway.
        if exc.code in (408, 429):
            raise IngestTransient(
                f"EE asked us to retry (HTTP {exc.code}): {exc.reason}"
            ) from exc
        if 400 <= exc.code < 500:
            raise IngestTerminal(
                f"EE rejected upload (HTTP {exc.code}): {exc.reason}"
            ) from exc
        raise IngestTransient(f"EE responded {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IngestTransient(f"EE request failed: {exc}") from exc
