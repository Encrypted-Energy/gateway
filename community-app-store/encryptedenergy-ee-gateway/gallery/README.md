# Gallery screenshots

umbrelOS shows the images in this folder on the EE Gateway app listing. The
filenames here must match the `gallery:` entries in
`../umbrel-app.yml` exactly, in this order:

1. `1-setup.png`     — the first-run setup wizard (org ID + API token form)
2. `2-dashboard.png` — the main dashboard showing recent packets
3. `3-status.png`    — the status / health view

## Current images

The three PNGs in this folder are generated, theme-accurate placeholders
(1280x800, matching the manifest and the live UI palette). They are
store-ready as is. To replace them with captures from your own running
gateway, follow the steps below and overwrite the files in place.

## How to produce them

Run the UI locally and screenshot each screen:

```sh
cd ../../../ui
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
python -m ee_gateway_ui.app
# open http://localhost:8080
```

Capture each screen, name the files exactly as above, drop them in this
folder, and delete this README (or leave it — umbrelOS ignores non-image
files here). Recommended size: 1280x800 PNG, matching the manifest.

> This file only exists so the otherwise-empty `gallery/` directory is kept
> when the repo is cloned/extracted. The icon and gallery URLs in
> `umbrel-app.yml` resolve to:
> `https://raw.githubusercontent.com/encryptedenergy/umbrel-community-app-store/main/encryptedenergy-ee-gateway/gallery/<name>.png`
> They will 404 until you push this repo with the PNGs added.
