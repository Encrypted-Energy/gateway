#!/bin/bash
# EE Gateway worker container entrypoint.
# Copyright (C) 2026 encryptedenergy.com
# SPDX-License-Identifier: GPL-3.0-only
#
# Two responsibilities, then we get out of the way:
#
#   1. Locate the GPS dongle and start gpsd against it. From 0.7.2 the
#      entrypoint auto-discovers across both common USB-serial families:
#        - /dev/ttyUSB* — PL2303-class dongles (BU-353-S4, BU-353N, etc.)
#        - /dev/ttyACM* — CDC-ACM dongles (u-blox NEO / VK-162 / VK-172)
#      gpsd accepts multiple devices and figures out which one is
#      actually emitting NMEA, so we just hand it everything we find.
#      EE_GPS_DEVICE is still honored as an explicit override when set
#      and the path exists; this preserves the 0.7.x contract for
#      operators with non-standard setups.
#
#   2. exec the Python worker as the unprivileged `ee` user (uid 1000).
#      Dropping privs here, not in the Dockerfile, lets step 1 run as
#      root (which it has to, to open the serial device).
#
# If no GPS device is present the worker still runs; its status flips
# to "dongle_missing" and the dashboard prompts the operator to plug
# one in. We never crash on a missing dongle, because the same image
# is used during fresh installs before hardware is wired up.

set -e

# Compute the device list:
#   1. If EE_GPS_DEVICE is explicitly set AND the path exists, that wins
#      (operator override).
#   2. Otherwise, glob for any plausible USB-serial device.
#   3. If neither produces anything, log and continue without gpsd.
DEVICES=""
if [ -n "$EE_GPS_DEVICE" ] && [ -e "$EE_GPS_DEVICE" ]; then
  DEVICES="$EE_GPS_DEVICE"
  echo "[entrypoint] using explicit GPS device override: $EE_GPS_DEVICE"
else
  # shellcheck disable=SC2086
  for candidate in /dev/ttyUSB* /dev/ttyACM*; do
    [ -e "$candidate" ] || continue
    DEVICES="$DEVICES $candidate"
  done
  DEVICES="${DEVICES# }"
fi

if [ -n "$DEVICES" ]; then
  # -n: start reading the device immediately, do not wait for the first
  #     client connect. Makes a fix available to our first heartbeat.
  # -G: allow connections from any address. We only bind 127.0.0.1 (the
  #     default), so this just means "let any process inside this
  #     container reach gpsd" — which is what we want.
  # shellcheck disable=SC2086
  if gpsd -n -G $DEVICES; then
    echo "[entrypoint] gpsd started on: $DEVICES"
  else
    echo "[entrypoint] gpsd failed to start on: $DEVICES; worker will report dongle_missing"
  fi
else
  echo "[entrypoint] no GPS device found (looked at /dev/ttyUSB* and /dev/ttyACM*); worker will report dongle_missing"
fi

# Hand off as uid 1000. The Python worker reaches the host bluetoothd over
# D-Bus and never needs root itself; this matches the 0.4.0 posture.
exec gosu ee:ee python -m ee_gateway_worker.main
