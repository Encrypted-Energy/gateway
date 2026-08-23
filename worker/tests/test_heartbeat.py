# EE Gateway worker, heartbeat payload tests.
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

"""Heartbeat body contract, focused on the 0.8.1 platform parity fields.

The iOS and Android apps send ``worker_version`` with an
``ee-gateway-<platform>/`` prefix plus explicit ``platform`` and
``os_version`` fields; from 0.8.1 the Umbrel worker follows the same
convention so the fleet reads one format. ee-web has accepted ``platform``
since 0.10.6 (the field was added anticipating exactly this change).
"""

import io
import json

from ee_gateway_worker import __version__ as WORKER_VERSION
from ee_gateway_worker import heartbeat


class _FakeResponse(io.BytesIO):
    """Minimal context-manager stand-in for urlopen's response."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_body(monkeypatch):
    """Patch urlopen to record the POSTed JSON body and return 200."""
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["url"] = request.full_url
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(heartbeat.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_worker_version_carries_umbrel_prefix(monkeypatch):
    captured = _capture_body(monkeypatch)
    result = heartbeat.report(base_url="https://ee.test", api_token="tok")
    assert result == {"ok": True}
    assert captured["body"]["worker_version"] == f"ee-gateway-umbrel/{WORKER_VERSION}"


def test_platform_is_umbrel(monkeypatch):
    captured = _capture_body(monkeypatch)
    heartbeat.report(base_url="https://ee.test", api_token="tok")
    assert captured["body"]["platform"] == "umbrel"


def test_os_version_present_and_capped(monkeypatch):
    captured = _capture_body(monkeypatch)
    heartbeat.report(base_url="https://ee.test", api_token="tok")
    os_version = captured["body"]["os_version"]
    assert isinstance(os_version, str)
    assert 0 < len(os_version) <= 64
