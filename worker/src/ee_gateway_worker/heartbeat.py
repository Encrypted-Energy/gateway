# EE Gateway worker, heartbeat client.
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

"""Periodic check-in to encryptedenergy.com.

The worker POSTs to ``${EE_BASE_URL}/api/v1/gateways/heartbeat`` so the EE
dashboard shows the gateway as alive. Authentication is a single Bearer
token: the EE-issued API token already stored in ``config.json``. The token
alone identifies the gateway, so no gateway id is sent in the URL or body.

Implemented with ``urllib.request`` from the standard library to avoid
adding a runtime dependency. All failures are logged at WARNING and never
raise: a missed heartbeat is a transient observability gap, not a reason
to stop scanning or ingesting.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

HEARTBEAT_PATH = "/api/v1/gateways/heartbeat"
REQUEST_TIMEOUT_SECONDS = 10

log = logging.getLogger("ee_gateway_worker.heartbeat")


def report(
    *,
    base_url: str,
    api_token: str,
    last_packet_at: str | None = None,
    last_known_position_at: str | None = None,
) -> dict | None:
    """Send one heartbeat. Returns the parsed JSON response, or ``None`` on failure.

    ``last_packet_at`` and ``last_known_position_at`` are optional ISO 8601
    UTC timestamps. Omit them when the worker has nothing new to report; the
    EE side leaves the stored value alone in that case.
    """
    url = base_url.rstrip("/") + HEARTBEAT_PATH

    body: dict = {}
    if last_packet_at:
        body["last_packet_at"] = last_packet_at
    if last_known_position_at:
        body["last_known_position_at"] = last_known_position_at

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ee-gateway-worker/heartbeat",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {"ok": True}
    except urllib.error.HTTPError as exc:
        # 401 means the token is invalid or revoked; that is worth flagging
        # loudly because the same token is also driving Hubble ingest, which
        # will start failing too.
        if exc.code == 401:
            log.warning("heartbeat rejected (401), token invalid or revoked")
        else:
            log.warning("heartbeat HTTP %d: %s", exc.code, exc.reason)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("heartbeat network error: %s", exc)
        return None
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("heartbeat response not JSON: %s", exc)
        return None
