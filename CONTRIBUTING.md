# Contributing to EE Gateway

Thanks for your interest. EE Gateway is early — v0, work in progress — so the
most useful contributions right now are bug reports from real hardware,
small fixes, and documentation. Please open an issue before starting
anything large; the [README](./README.md) `Scope (v0)` section says what is
deliberately out of scope for now.

## Repository layout

The project is two independent containers that share a data directory but no
code:

- `worker/` — the privileged BLE scanner and cloud ingester (`pyhubblenetwork`).
- `ui/` — the unprivileged Flask setup wizard and dashboard.

Each is its own Python package with its own `pyproject.toml` and tests. The
only thing they share is the `packet_log` SQLite schema, which is re-declared
on each side on purpose — see the comments in `worker/db.py` and `ui/.../app.py`.

## Development setup

Python 3.11 or newer. Work on one container at a time, each in its own
virtual environment:

```sh
# worker
cd worker
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest

# ui
cd ui
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

To run the UI against real data, point it at a directory that already holds a
worker's `config.json` / `state.json` / `packets.db`:

```sh
EE_DATA_DIR=/path/to/data python -m ee_gateway_ui.app
# then open http://localhost:8080
```

## Pull requests

- Keep changes small and focused; one concern per PR.
- Every PR must keep `pytest` green for any package it touches, and add tests
  for new behavior.
- Match the surrounding style. The worker and UI both favor small, well-named
  functions and comments that explain *why*, not *what*.
- Update `CHANGELOG.md` under `[Unreleased]` when behavior changes.
- If you change the `packet_log` schema, change it — and its re-declarations —
  on both sides, or the containers will silently disagree.

## Licensing of contributions

EE Gateway is licensed under [GPL-3.0-only](./LICENSE). By submitting a
contribution you agree that it is licensed under those same terms. New source
files should carry the short GPLv3 header used by existing files in the
package you are editing.

## Code of conduct

Participation in this project is governed by the
[Code of Conduct](./CODE_OF_CONDUCT.md).
