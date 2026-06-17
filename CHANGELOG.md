# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Gateway 0.8.0 (worker 0.7.2 + UI 0.4.1): two QoL improvements bundled.
  Worker entrypoint now auto-discovers the GPS dongle across both common
  USB-serial families — `/dev/ttyUSB*` (PL2303-class) and `/dev/ttyACM*`
  (CDC-ACM / u-blox). Operators with either dongle family Just Work
  with no compose override; `EE_GPS_DEVICE` still wins when set and the
  path exists. UI dashboard header reads the version from the
  `EE_GATEWAY_VERSION` env var (passed by the compose) instead of a
  hardcoded string, so the badge stays accurate across releases.
- Worker 0.7.1: stationary-location override. Two env vars
  (`EE_GPS_FIXED_LAT`, `EE_GPS_FIXED_LON`) let an operator pin the gateway
  to a known coordinate, bypassing gpsd entirely. The worker stamps every
  packet with the configured coordinate and reports `gps_status="fix"` in
  heartbeats. Off by default; both env vars must be set to floats to
  activate (a misconfig is logged and ignored, never crashes the worker).
  Use cases: indoor-mounted / kiosk gateways where GPS can't see sky,
  development testing, or keeping the ingest pipeline running while a
  replacement dongle ships. App manifest moves to 0.7.1; worker image
  bumps to 0.7.1; UI stays at 0.4.0.
- Worker 0.7.0: fleet telemetry. Two new counter deltas in the heartbeat
  (`packets_heard_delta`, `ble_scan_errors_delta`) ride the existing
  snapshot/restore pattern, so a failed heartbeat never loses counts. Two
  new self-description fields (`worker_version`, `uptime_seconds`) let
  encryptedenergy.com chart rollout adoption and detect flapping
  gateways. Backward-compatible: pre-0.7.0 ee-web silently ignores the
  new fields. App manifest moves to 0.7.0; worker image bumps to 0.7.0;
  UI stays at 0.4.0.
- Project scaffold: GPLv3 license, repository layout.
- Worker packet store (`worker/db.py`): SQLite `packet_log` table in WAL mode
  with insert, pending-queue, ingest-status, and aggregate-read helpers.
- Worker config loader (`worker/config.py`): resolves Hubble credentials and
  scan timings with env > `config.json` > default precedence, validates them,
  and tolerates a missing or half-written file.
- Worker entry point (`worker/main.py`): a BLE scan loop and a separate cloud
  ingest thread sharing one WAL database; flattens the SDK's four packet types
  into rows and ingests `EncryptedPacket` packets; writes `state.json` and
  shuts down cleanly on SIGTERM.
- `packet_log` gains a nullable `packet_type` column recording the SDK class
  name of each scanned packet.
- Worker container image (`worker/Dockerfile`): Python 3.12 slim base, builds
  for arm64 and amd64, runs as a non-root user, and carries GPL-3.0 OCI labels.
- UI application (`ui/`): an unprivileged Flask app that pairs a credentials
  setup wizard with a read-only dashboard. It writes `config.json` atomically,
  reads the worker's `state.json` for status and counts, and opens `packets.db`
  strictly read-only to list devices in range. It shares no code with the
  worker — the `packet_log` schema is the only contract between them.
- UI styling (`ui/.../static/style.css`): a self-contained instrument-panel
  theme — no framework, no build step.
- Contributor guide (`CONTRIBUTING.md`) and a Contributor Covenant
  `CODE_OF_CONDUCT.md`.

### Changed
- Worker quiets the Hubble SDK's httpx request logging to WARNING so the
  gateway log is not flooded with one line per cloud call.
