# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Gateway 0.10.6 (worker 0.7.7 + UI 0.6.4): the fixed-location flow —
  the majority setup path now that the official Umbrel store is the
  primary funnel and most operators have no GPS dongle — gets three
  coordinated fixes, plus a worker reliability fix.
  (1) Address geocoding: both location forms (setup step 2 and
  /settings/location) grow an "Address or place" field with a "Find
  coordinates" button. It resolves via OpenStreetMap's public Nominatim
  (no API key; called only on operator click, well inside the usage
  policy), fills the coordinate fields for review, and never saves
  without explicit confirmation. Lookup failures degrade to manual
  entry with an actionable message.
  (2) Coordinate parsing now accepts what operators actually paste:
  the full "lat, lon" pair from Google Maps' right-click copy dropped
  into either field (it wins over the other field, including the
  North-Pole prefill), degree symbols, N/S/E/W hemisphere letters
  (S/W flip the sign), Unicode minus (Wikipedia), EU decimal commas
  ("40,7128" — disambiguated from pair pastes, which always carry a
  dot, a space, or a degree mark), and stray trailing commas from
  hand-split pairs. Degrees-minutes-seconds still rejects, now with an
  error that shows the decimal format. Before this, the "must be
  numeric" rejection fired on the exact paste flow the form's own hint
  recommended.
  (3) Org ID removed from setup: the ee_live API token alone
  authenticates, so the organization ID field (never consumed by any
  code path) is gone from the wizard. The worker no longer requires
  org_id in config.json; a legacy key from an older install is
  preserved on disk and ignored. Existing installs keep working
  unchanged.
  (4) Retryable upstream errors: the worker's ingest client now treats
  HTTP 408 (request timeout) and 429 (rate limit) from EE as
  transient — the packet stays pending and retries on the next ingest
  pass instead of being classified terminal and dropped permanently.
  New `worker/tests/test_ee_client.py` covers the transient / terminal
  / unauthorized mapping.
  Also re-syncs `worker/pyproject.toml` (0.7.4 -> 0.7.7) and
  `ui/pyproject.toml` (0.6.2 -> 0.6.4) with their `__init__.py`
  `__version__` values, which had drifted.
- Gateway 0.10.5 (worker 0.7.6 + UI 0.6.3): (0.0, 0.0) fixed-location
  fix, both sides. The UI's coordinate parser rejects (0, 0) with an
  actionable error (it is the classic "unset override" tell and every
  packet stamped with it is rejected upstream with an opaque 422), and
  the worker's GpsClient ignores a (0, 0) it finds in config.json and
  falls back to gpsd, as defense in depth. Also adds the EE_GPS_BAUD
  env var, a baud hint for gpsd for u-blox M10-based receivers that
  default to 38400 (auto-probe can miss it); unset by default so
  standard consumer dongles keep working via auto-probe.
- Gateway 0.10.4 (worker 0.7.5): security hardening. The bundled gpsd
  no longer starts with -G, so it binds 127.0.0.1:2947 (loopback only)
  instead of 0.0.0.0:2947. Because the worker runs with network_mode:
  host, -G was exposing a LAN-facing GPS listener with no legitimate
  consumer. The worker reads gpsd over loopback; no functional change.
  UI unchanged.
- Gateway 0.10.3: manifest-only listing rebrand. Tagline and
  description repositioned as a generic self-hosted Bluetooth gateway
  for open BLE networks, with Hubble Network named as the first
  supported upstream. No image rebuilds; worker and UI unchanged.
- Gateway 0.10.2 (UI 0.6.2): polish pass on the setup wizard and
  dashboard. Setup step 2 pre-fills the North Pole (90, 0) as an
  obvious placeholder (replaces the 0.10.1 attempt at Hubble HQ,
  which looked too much like a real location); EE's worker
  already uses (90, 0) as its "no real location" sentinel, so an
  unedited gateway is obviously spottable on any future coverage
  map. Copy across setup.html, setup_location.html, and
  settings_location.html rewritten to sound conversational and to
  drop em dashes (and other artifacts that flagged on a copy-rules
  pass). Dashboard KPIs render with thousand-separator commas now
  (16,923 instead of 16923) via a new Jinja `thousands` filter.
  Packets tile sublabel changed from "captured this gateway" to
  "captured in last 30 days" so the number is read against the
  worker's actual retention window. Worker unchanged.
- Gateway 0.10.1 (UI 0.6.1): polish on the location pages added in
  0.10.0. Setup step 2 now pre-fills Hubble Network's Seattle HQ
  (47.61430270391947, -122.3191470665486) as a sensible default
  value (operators can save as-is or replace with their real
  coordinates). Copy on the post-setup `/settings/location` page
  rewritten to drop the awkward "where this gateway lives"
  phrasing and clarify the GPS-dongle fallback ("To switch over
  to a connected GPS dongle instead, clear both fields"). Field
  placeholders on both pages updated to Hubble HQ coords. Worker
  unchanged.
- Gateway 0.10.0 (UI 0.6.0): two-step setup with real credential
  verification. Setup is now (1) credentials and (2) location, in
  that order. Step 1 calls ee-web's new `/api/v1/gateways/verify`
  endpoint synchronously and surfaces bad tokens inline instead of
  silently saving and failing ~30s later on the dashboard's
  auth_error badge. Step 2 is a required lat/lon entry — operators
  can no longer reach the dashboard with no location set (which
  caused the worker to silently drop every packet on the "no GPS
  fix" path). The `/advanced` route is renamed to
  `/settings/location` (old URL preserved as a 308 redirect for
  any operator with a bookmark). Dashboard's "Location override"
  pill softened to "Location" now that operator-declared location
  is the primary path, not an override on top of GPS. Worker
  unchanged at 0.7.4.
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
