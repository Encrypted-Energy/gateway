# Encrypted Energy — Umbrel Community App Store

A [community app store](https://github.com/getumbrel/umbrel-community-app-store)
for [umbrelOS](https://umbrel.com), publishing apps from
[encryptedenergy.com](https://encryptedenergy.com).

## Apps

| App | ID | Description |
| --- | --- | --- |
| **EE Gateway** | `encryptedenergy-ee-gateway` | Self-hosted Hubble Network Bluetooth gateway |

## Add this store to your Umbrel

1. Open your umbrelOS dashboard.
2. Go to the **App Store**, click the **⋯** menu (top right), and choose
   **Community App Stores**.
3. Paste this repository's GitHub URL and click **Add**.
4. "Encrypted Energy App Store" appears; install **EE Gateway** from there.

## Repository requirements (read before pushing)

The app's icon and gallery images are referenced by **absolute** raw-GitHub
URLs inside `encryptedenergy-ee-gateway/umbrel-app.yml`, of the form:

```
https://raw.githubusercontent.com/encryptedenergy/umbrel-community-app-store/main/encryptedenergy-ee-gateway/<path>
```

For those URLs to resolve, this repository **must** be:

- under the GitHub org/user **`encryptedenergy`**,
- named exactly **`umbrel-community-app-store`**,
- with its default branch named **`main`**.

If you use a different org, repo name, or branch, update the `icon:` and
`gallery:` URLs in `encryptedenergy-ee-gateway/umbrel-app.yml` to match, or the
images will 404 in the umbrelOS UI.

## Layout

```
umbrel-app-store.yml                      # store id ("encryptedenergy") + name
encryptedenergy-ee-gateway/
  umbrel-app.yml                          # app listing metadata
  docker-compose.yml                      # app services (ui + worker behind app_proxy)
  icon.svg                                # app icon
  gallery/                                # listing screenshots (add the PNGs)
    1-setup.png  2-dashboard.png  3-status.png
```

## Keeping in sync with the app repo

`encryptedenergy-ee-gateway/umbrel-app.yml`, `docker-compose.yml`, and
`icon.svg` are **copies** of the files in the
[ee-gateway](https://github.com/encryptedenergy/ee-gateway) repo's `umbrel/`
directory, which is the source of truth. When you change the app's Umbrel
packaging, edit it in `ee-gateway/umbrel/`, then copy the three files here and
bump `version:` in both places. The current published version is **0.1.0**.

## License

The packaging in this repository is GPL-3.0-only, matching the EE Gateway app.
EE Gateway is an independent, community-built integration for the Hubble
Network and is not an official Hubble product.
