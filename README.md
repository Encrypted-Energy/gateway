# EE Gateway

A self-hosted Bluetooth-LE gateway that runs as an [Umbrel](https://umbrel.com)
app. Point it at a machine with a Bluetooth radio and it scans for nearby BLE
devices from supported open networks and forwards their packets to the right
upstream cloud, turning any always-on Umbrel into a ground node.

[Hubble Network](https://hubblenetwork.com) is the first upstream we support.
More partner networks will be added as they come online.

> **Status: v0, work in progress.** Not yet published to the Umbrel App Store.

## How it works

Two containers:

- **worker** — privileged (`NET_ADMIN` + `NET_RAW`, host network, read-only
  D-Bus). Runs a BLE scan loop, stores packets in SQLite, and ingests them
  to the relevant upstream cloud. Hubble Network support is implemented
  against [`pyhubblenetwork`](https://github.com/HubbleNetwork/pyhubblenetwork);
  additional networks will live alongside it as we add them.
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
