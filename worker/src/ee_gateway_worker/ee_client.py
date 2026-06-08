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

This module exposes one function (:func:`ingest_packet`) that the
``ingest_loop`` in ``main.py`` calls per packet. Implemented with
``urllib.request`` to keep the worker free of new dependencies.

Failure modes returned to the caller:

* :class:`IngestTransient`  the request never reached EE, or EE returned 5xx.
  The caller should leave the packet pending and retry on the next pass.
* :class:`IngestTerminal`   EE returned 4xx (bad token, malformed body, etc).
  The caller should drop the packet to clear it from the queue.
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


def ingest_packet(
    *,
    base_url: str,
    api_token: str,
    payload_b64: str,
    rssi: int | None,
    timestamp: int | None,
    latitude: float = 90.0,
    longitude: float = 0.0,
) -> None:
    """POST one packet to EE for forwarding to Hubble.

    Coordinates default to (90, 0) for parity with the Hubble SDK's
    placeholder. They will be replaced by live GPS reads in Gateway 0.5.0.
    """
    url = base_url.rstrip("/") + PACKETS_PATH

    body = {
        "packets": [
            {
                "payload_b64": payload_b64,
                "rssi": rssi,
                "timestamp": timestamp,
                "latitude": latitude,
                "longitude": longitude,
            }
        ]
    }

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
                f"EE rejected packet (HTTP 401): token invalid or revoked"
            ) from exc
        if 400 <= exc.code < 500:
            raise IngestTerminal(
                f"EE rejected packet (HTTP {exc.code}): {exc.reason}"
            ) from exc
        raise IngestTransient(f"EE responded {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise IngestTransient(f"EE request failed: {exc}") from exc
