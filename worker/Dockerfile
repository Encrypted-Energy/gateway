# EE Gateway — worker container
# Copyright (C) 2026 encryptedenergy.com
# SPDX-License-Identifier: GPL-3.0-only
#
# Scans for Hubble Network BLE packets and forwards them to
# encryptedenergy.com (which proxies to the Hubble cloud). 0.5.0 also
# embeds a local gpsd so the worker can stamp each packet with a real GPS
# fix from a USB GPS dongle. Built for linux/arm64 (Raspberry Pi / Umbrel
# Home) and linux/amd64.

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="EE Gateway Worker"
LABEL org.opencontainers.image.description="Hubble Network BLE gateway — scan, GPS, and cloud ingest worker"
LABEL org.opencontainers.image.source="https://github.com/encryptedenergy/ee-gateway"
LABEL org.opencontainers.image.licenses="GPL-3.0-only"

# PYTHONUNBUFFERED: stream logs to Docker immediately, so `docker logs` and the
#   Umbrel log view update in real time instead of buffering.
# PYTHONDONTWRITEBYTECODE: keep the image free of .pyc files.
# EE_DATA_DIR: shared Umbrel app-data dir holding config.json, packets.db,
#   and state.json. The UI container mounts the same path.
# EE_GPS_DEVICE: serial device the entrypoint passes to gpsd. Defaults to
#   /dev/ttyUSB0 (PL2303-class USB GPS like the GlobalSat BU-353-S4);
#   override via the compose `environment:` if your dongle is on
#   /dev/ttyACM0 (u-blox NEO USB) or an unusual channel of a multi-port
#   FTDI bridge (u-blox EVK-M102 GNSS UART sits on channel C = ttyUSB2).
# EE_GPS_BAUD: optional baud rate hint for gpsd. Unset by default so
#   gpsd auto-probes (works for BU-353N at 4800, VK-172 at 9600, etc.).
#   Set to 38400 for u-blox M10 chips (the M10 default) — gpsd's
#   auto-probe order can miss 38400 and sit silently on the port
#   without ever emitting NMEA.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EE_DATA_DIR=/data \
    EE_GPS_DEVICE=/dev/ttyUSB0

# ca-certificates: needed for HTTPS to encryptedenergy.com.
# gpsd + gpsd-clients: local GPS daemon and gpspipe (the latter is only used
#   for debugging; pulled in by the gpsd-clients meta).
# gosu: tiny setuid binary used by the entrypoint to drop from root (needed
#   to open the GPS serial device) to the unprivileged ee user before exec'ing
#   the Python worker.
# No BlueZ packages are installed: BLE scanning goes through the host's
# bluetoothd over the D-Bus system bus socket bind-mounted at /var/run/dbus
# by docker-compose.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         ca-certificates \
         gpsd \
         gpsd-clients \
         gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the worker package and its dependencies (pyhubblenetwork,
# python-json-logger) from pyproject.toml. The transitive native dependency
# (dbus-fast) ships prebuilt arm64 and amd64 wheels, so no compiler toolchain
# is needed in the image.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Create the unprivileged user the Python process will run as. We no longer
# `USER ee` at the Dockerfile level because the entrypoint needs to start
# gpsd as root (to open the serial device, then gpsd self-drops to nobody)
# before dropping to ee with gosu.
RUN useradd --create-home --uid 1000 ee \
    && mkdir -p /data \
    && chown -R ee:ee /data

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/data"]

# Entry: launch gpsd (if a GPS device is present), then exec the Python
# worker as uid 1000.
ENTRYPOINT ["/entrypoint.sh"]
