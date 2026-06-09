#!/bin/bash
# EE Gateway worker container entrypoint.
# Copyright (C) 2026 encryptedenergy.com
# SPDX-License-Identifier: GPL-3.0-only
#
# Two responsibilities, then we get out of the way:
#
#   1. If a GPS serial device is present at $EE_GPS_DEVICE, start gpsd
#      against it. gpsd binds 127.0.0.1:2947 inside this container; the
#      Python worker's gps.py module reads it over that socket. gpsd
#      itself drops to user `nobody` after opening the device, so it
#      does not stay root.
#
#   2. exec the Python worker as the unprivileged `ee` user (uid 1000).
#      Dropping privs here, not in the Dockerfile, lets step 1 run as
#      root (which it has to, to open the serial device).
#
# If the GPS device is missing the worker still runs; its status flips
# to "dongle_missing" and the dashboard prompts the operator to plug
# one in. We never crash on a missing dongle, because the same image
# is used during fresh installs before hardware is wired up.

set -e

GPS_DEVICE="${EE_GPS_DEVICE:-/dev/ttyUSB0}"

if [ -e "$GPS_DEVICE" ]; then
  # -n: start reading the device immediately, do not wait for the first
  #     client connect. Makes a fix available to our first heartbeat.
  # -G: allow connections from any address. We only bind 127.0.0.1 (the
  #     default), so this just means "let any process inside this
  #     container reach gpsd" — which is what we want.
  if gpsd -n -G "$GPS_DEVICE"; then
    echo "[entrypoint] gpsd started on $GPS_DEVICE"
  else
    echo "[entrypoint] gpsd failed to start on $GPS_DEVICE; worker will report dongle_missing"
  fi
else
  echo "[entrypoint] no GPS device at $GPS_DEVICE; worker will report dongle_missing"
fi

# Hand off as uid 1000. The Python worker reaches the host bluetoothd over
# D-Bus and never needs root itself; this matches the 0.4.0 posture.
exec gosu ee:ee python -m ee_gateway_worker.main
