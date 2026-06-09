# EE Gateway worker — in-memory heartbeat counters.
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

"""Thread-safe in-memory counters reported as deltas in each heartbeat.

The server keeps the running totals (``packets_forwarded_total``,
``packets_dropped_no_fix_total``). The worker only tracks *changes since
the last successful heartbeat*. This avoids three problems at once:

* a worker restart never double-counts: a fresh worker starts at 0 and
  the server's total is unchanged;
* a transient HTTP failure does not lose counts: on failure the snapshot
  is restored to the live counter so the next heartbeat carries them;
* the server can use a single atomic ``UPDATE ... = ... + delta`` and
  never has to do client-side arithmetic.

Counters are bumped from the scan loop and snapshotted by the heartbeat
loop; the lock guards both paths.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class CountersSnapshot:
    """A frozen view of the counters at a moment in time."""

    forwarded: int = 0
    dropped_no_fix: int = 0


class CountersStore:
    """The live counters. Increments are O(1) and lock-bounded."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._forwarded = 0
        self._dropped_no_fix = 0

    def add_forwarded(self, n: int = 1) -> None:
        if n <= 0:
            return
        with self._lock:
            self._forwarded += n

    def add_dropped_no_fix(self, n: int = 1) -> None:
        if n <= 0:
            return
        with self._lock:
            self._dropped_no_fix += n

    def snapshot_and_reset(self) -> CountersSnapshot:
        """Take a snapshot of the current deltas and reset them to zero.

        The caller is expected to send the snapshot to the server and call
        :meth:`restore` if the server does not accept it.
        """
        with self._lock:
            snap = CountersSnapshot(self._forwarded, self._dropped_no_fix)
            self._forwarded = 0
            self._dropped_no_fix = 0
            return snap

    def restore(self, snap: CountersSnapshot) -> None:
        """Add the snapshot's values back into the live counters.

        Used when a heartbeat POST fails: the next heartbeat will carry
        these counts plus anything that has happened since.
        """
        with self._lock:
            self._forwarded += snap.forwarded
            self._dropped_no_fix += snap.dropped_no_fix
