# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Gateway 0.9.1 (UI 0.5.1): dashboard readability pass. KPI tiles now
  carry one-line subtitles so each number reads as meaning instead of
  code: "Devices in range — heard in last 24h", "Packets — captured
  this gateway", "Ingested — forwarded to Hubble", "Pending — waiting
  to forward". The previous "In range" label was ambiguous (devices?
  packets?); now matches the table heading below. Last-update time
  switches from absolute timestamp to relative phrase ("a minute ago")
  with absolute on hover. When the fixed-location override is active,
  the dashboard surfaces a quiet badge showing the pinned coordinate
  with a Change link, so the GPS pinning is no longer invisible from
  the main view. /advanced page's "Back to dashboard" button demoted
  to a centered link so it stops wrapping onto two lines on narrow
  cards. Worker unchanged.
- Gateway 0.9.0 (worker 0.7.4 + UI 0.5.0): persistent fixed-location
  override via the dashboard. New UI route `/advanced` with a form
  accepting decimal-degree latitude / longitude, range-validated
  server-side (-90..90 / -180..180), persisted to `config.json` in
  the persistent `/data` volume. Config.json survives Umbrel app
  updates (compose-only file gets overwritten on update; the data
  volume does not), so operators no longer need to re-add env-var
  overrides every release. Worker config loader gains `fixed_lat`
  / `fixed_lon` fields with the same validation; precedence is env
  var (legacy 0.7.1 path) -> config.json (new persistent path) ->
  no override. A successful save touches the restart sentinel so
  the new mode takes effect on the next worker boot.
- Gateway 0.8.1 (worker 0.7.3 + UI 0.4.1): worker now forwards each
  packet's device EID (hex string) to EE in the ingest body. EE uses
  it server-side to enforce a 1-bounty-per-(device, org, day) cap as
  an anti-abuse measure (a bad actor running many gateways covering
  the same device can no longer credit-stack on that device's many
  distinct packets). Body field is additive; older EE versions
  silently ignore the unknown field, so the worker stays
  forward-compatible.
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
