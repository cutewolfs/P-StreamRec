# P-StreamRec — Umbrel App

This directory contains the Umbrel packaging for [P-StreamRec](https://github.com/raccommode/P-StreamRec).

## Install on Umbrel

1. Open the **App Store** on your Umbrel
2. Click the store menu → **Community App Stores → Add**
3. Paste: `https://github.com/raccommode/P-StreamRec`
4. Install **P-StreamRec**

## What's inside

- `umbrel-app.yml` — app manifest (metadata, port, gallery)
- `docker-compose.yml` — two services: `web` (P-StreamRec) and `flaresolverr` (Cloudflare bypass)
- `icon.svg` — app icon

## Notes on the Umbrel build

Compared to the standalone `docker-compose.yml` at the repo root, the Umbrel version:

- Uses Umbrel's `app_proxy` service (authentication handled by Umbrel)
- Mounts recordings on `${APP_DATA_DIR}/data` instead of a relative `./data`
- Does **not** mount `/var/run/docker.sock` — Umbrel handles app updates natively; the in-app "Update" button will display the manual commands fallback, which you can safely ignore
- Runs FlareSolverr as a sibling container (`p-streamrec-store-p-streamrec_flaresolverr_1`) on the app's internal network — no host port is exposed
- Port `8080` is the one exposed to users via `app_proxy`

## Configuration

To set provider credentials, a `PASSWORD`, `AUTO_RECORD_USERS`, the optional DNS cache (`PSTREAMREC_DNS_CACHE=true`), Playwright browser capture settings (`PSTREAMREC_BROWSER_HEADLESS`, `PSTREAMREC_BROWSER_CAPTURE_TIMEOUT`), or an outbound proxy (`PSTREAMREC_PROXY_URL`, `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`), edit the `environment:` section of `docker-compose.yml` in the installed app directory on your Umbrel (`~/umbrel/app-data/p-streamrec-store-p-streamrec/`) and restart the app from the UI.

## Updating

When a new version is released, Umbrel will show an **Update** badge in the App Store. Click it — Umbrel will pull the new image and recreate the container automatically.

## License

Non-Commercial Open Source — see [LICENSE](../LICENSE).
