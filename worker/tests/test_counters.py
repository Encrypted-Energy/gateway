# EE Gateway worker — tests for CountersStore.
# Copyright (C) 2026 encryptedenergy.com
# Licensed under the GNU General Public License version 3 (GPL-3.0-only).
# See the LICENSE file at the repository root.

"""Tests for ee_gateway_worker.counters.

The store backs the heartbeat delta-and-reset flow. Two properties matter:

* a snapshot leaves the live counters at zero so the next heartbeat does
  not double-count;
* a restored snapshot adds its values back to the live counters so a
  failed POST does not lose counts.
"""

from ee_gateway_worker.counters import CountersStore


def test_add_and_snapshot_reset_to_zero():
    store = CountersStore()
    store.add_forwarded(3)
    store.add_dropped_no_fix(2)
    store.add_heard(7)
    store.add_ble_scan_error()
    snap = store.snapshot_and_reset()
    assert snap.forwarded == 3
    assert snap.dropped_no_fix == 2
    assert snap.heard == 7
    assert snap.ble_scan_errors == 1
    # After the snapshot, a fresh snapshot must come back empty.
    empty = store.snapshot_and_reset()
    assert empty.forwarded == 0
    assert empty.dropped_no_fix == 0
    assert empty.heard == 0
    assert empty.ble_scan_errors == 0


def test_restore_puts_counts_back_for_next_heartbeat():
    store = CountersStore()
    store.add_forwarded(5)
    store.add_heard(10)
    store.add_ble_scan_error(2)
    snap = store.snapshot_and_reset()
    # Something happens between snapshot and POST.
    store.add_forwarded(2)
    store.add_heard(3)
    store.add_ble_scan_error()
    # POST fails; caller restores. The next snapshot must carry both.
    store.restore(snap)
    next_snap = store.snapshot_and_reset()
    assert next_snap.forwarded == 7
    assert next_snap.heard == 13
    assert next_snap.ble_scan_errors == 3


def test_add_ignores_non_positive_values():
    store = CountersStore()
    store.add_forwarded(0)
    store.add_forwarded(-1)
    store.add_dropped_no_fix(-5)
    store.add_heard(0)
    store.add_heard(-2)
    store.add_ble_scan_error(0)
    store.add_ble_scan_error(-1)
    snap = store.snapshot_and_reset()
    assert snap.forwarded == 0
    assert snap.dropped_no_fix == 0
    assert snap.heard == 0
    assert snap.ble_scan_errors == 0
