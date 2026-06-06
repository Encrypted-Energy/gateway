# EE Gateway — worker container
# Copyright (C) 2026 encryptedenergy.com
# SPDX-License-Identifier: GPL-3.0-only
#
# Scans for Hubble Network BLE packets and ingests them to the Hubble cloud.
# Built for linux/arm64 (Raspberry Pi / Umbrel Home) and linux/amd64.

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="EE Gateway Worker"
LABEL org.opencontainers.image.description="Hubble Network BLE gateway — scan and cloud ingest worker"
LABEL org.opencontainers.image.source="https://github.com/encryptedenergy/ee-gateway"
LABEL org.opencontainers.image.licenses="GPL-3.0-only"

# PYTHONUNBUFFERED: stream logs to Docker immediately, so `docker logs` and the
#   Umbrel log view update in real time instead of buffering.
# PYTHONDONTWRITEBYTECODE: keep the image free of .pyc files.
# EE_DATA_DIR: shared Umbrel app-data dir holding config.json, packets.db,
#   and state.json. The UI container mounts the same path.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EE_DATA_DIR=/data

# ca-certificates is needed for the HTTPS calls to api.hubble.com.
# No BlueZ packages are installed in the image on purpose: BLE scanning is done
# by the host's bluetoothd, which the worker reaches over the D-Bus system bus
# socket bind-mounted at /var/run/dbus by docker-compose. The Python BLE stack
# (bleak -> dbus-fast) speaks the D-Bus wire protocol directly, so the
# container needs no Bluetooth userspace of its own.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the worker package and its dependencies (pyhubblenetwork,
# python-json-logger) from pyproject.toml. The transitive native dependency
# (dbus-fast) ships prebuilt arm64 and amd64 wheels, so no compiler toolchain
# is needed in the image.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Run as a non-root user. The privileged BLE work happens in the host
# bluetoothd over D-Bus, so the container process itself never needs root.
# uid 1000 matches Umbrel's convention.
RUN useradd --create-home --uid 1000 ee \
    && mkdir -p /data \
    && chown -R ee:ee /data
USER ee

VOLUME ["/data"]

# main() starts the cloud-ingest thread and runs the BLE scan loop on the main
# thread until it receives SIGTERM/SIGINT.
ENTRYPOINT ["python", "-m", "ee_gateway_worker.main"]
