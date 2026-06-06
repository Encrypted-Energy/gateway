# EE Gateway

A self-hosted [Hubble Network](https://hubblenetwork.com) Bluetooth-LE gateway
that runs as an [Umbrel](https://umbrel.com) app. Point it at a machine with a
Bluetooth radio and it scans for nearby Hubble devices and forwards their
packets to the Hubble cloud — turning any always-on Umbrel into a gateway.

> **Status: v0, work in progress.** Not yet published to the Umbrel App Store.

## How it works

Two containers:

- **worker** — privileged (`NET_ADMIN` + `NET_RAW`, host network, read-only
  D-Bus). Runs a [`pyhubblenetwork`](https://github.com/HubbleNetwork/pyhubblenetwork)
  scan loop, stores packets in SQLite, and ingests them to the Hubble cloud.
- **ui** — unprivileged Flask dashboard behind Umbrel's `app_proxy`. Setup
  wizard for credentials, plus a view of what the gateway is seeing.

State lives in the shared app data directory: `config.json` (credentials),
`state.json` (worker status), `packets.db` (SQLite, WAL mode).

## Scope (v0)

In: BLE scan → ingest, local dashboard, Umbrel packaging, Raspberry Pi 5.
Out: Lightning payouts, gateway identity/certificates, a public registry,
satellite mode. Those are later work.

## License

[GPL-3.0-only](./LICENSE). `pyhubblenetwork` is Apache-2.0, which is
one-way compatible with GPLv3.
