# EE Gateway — UI container
# Copyright (C) 2026 encryptedenergy.com
# SPDX-License-Identifier: GPL-3.0-only
#
# Unprivileged Flask setup wizard and dashboard for the EE Gateway. Runs
# behind Umbrel's app_proxy. Built for linux/arm64 (Raspberry Pi / Umbrel
# Home) and linux/amd64.

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="EE Gateway UI"
LABEL org.opencontainers.image.description="Hubble Network BLE gateway — setup wizard and dashboard"
LABEL org.opencontainers.image.source="https://github.com/encryptedenergy/ee-gateway"
LABEL org.opencontainers.image.licenses="GPL-3.0-only"

# PYTHONUNBUFFERED: stream logs to Docker immediately, so `docker logs` and the
#   Umbrel log view update in real time instead of buffering.
# PYTHONDONTWRITEBYTECODE: keep the image free of .pyc files.
# EE_DATA_DIR: shared Umbrel app-data dir holding config.json, packets.db,
#   and state.json. The worker container mounts the same path; the UI reads
#   state.json and packets.db from it and writes config.json into it.
# EE_UI_PORT: port the Flask server listens on; app_proxy targets it.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EE_DATA_DIR=/data \
    EE_UI_PORT=8080

# No apt layer on purpose. The UI needs no Bluetooth userspace and no compiler
# toolchain (Flask installs from a pure-Python wheel). It DOES make outbound
# HTTPS calls — credential verification against encryptedenergy.com (0.6.0+)
# and address geocoding against nominatim.openstreetmap.org (0.6.4+) — which
# rely on the ca-certificates bundle already present in the slim base image.
# If the base image ever drops it, add an apt layer installing ca-certificates.

WORKDIR /app

# Install the UI package and its dependency (flask) from pyproject.toml. The
# Jinja templates and CSS are declared as package data, so `pip install .`
# bundles them into the installed package where Flask's loader finds them.
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Run as a non-root user. The UI never needs elevated privileges: it touches
# no Bluetooth and no host network. uid 1000 matches Umbrel's convention and
# the worker image, so both containers write the shared data dir as the same
# owner.
RUN useradd --create-home --uid 1000 ee \
    && mkdir -p /data \
    && chown -R ee:ee /data
USER ee

VOLUME ["/data"]

EXPOSE 8080

# main() runs Flask's built-in server on 0.0.0.0:${EE_UI_PORT}. Adequate for a
# single-user dashboard behind app_proxy; a production WSGI server is v1.
ENTRYPOINT ["python", "-m", "ee_gateway_ui.app"]
