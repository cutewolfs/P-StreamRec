# P-StreamRec

[![License: Non-Commercial](https://img.shields.io/badge/License-Non--Commercial-red.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Ready-blue.svg)](https://www.docker.com/)
[![Open Source](https://img.shields.io/badge/Open%20Source-Yes-green.svg)](https://github.com/raccommode/P-StreamRec)

**Watch, record, and follow live cam streams across integrated providers + any m3u8 source — with a modern web interface.**

## Features

- **24/7 automatic recording** — monitors models and records when they go live
- **Recording segmentation** — optionally split captures by 30/60/90 minutes or by maximum file size
- **Auto MP4 conversion** — converts TS to compressed MP4 in background (50-70% smaller)
- **Discover** — browse live models from every supported site with gender, tag, and search filters
- **Following** — one list aggregating your follows across providers
- **Recordings** — manage all recordings with built-in video player
- **Live Watch** — watch streams directly in the browser with HLS player
- **Account login** — provider sessions are stored as cookies/localStorage, never passwords
- **Protected providers** — Playwright browser capture plus yt-dlp extractors for JS/Cloudflare-heavy sites
- **Settings** — manage accounts, FlareSolverr status, tag blacklist
- **Password protection** — optional login to secure the interface
- **GitOps updates** — update the app directly from the UI
- **Docker ready** — one command to get started

## Supported sites

| Site | Discover | Watch | Record | Follow |
|------|:--------:|:-----:|:------:|:------:|
| **Chaturbate** | ✅ | ✅ | ✅ | ✅ |
| **CAM4** | ✅ | ✅ | ✅ | ✅ |
| Stripchat | ✅ | ✅ | ✅ | Local |
| BongaCams | ✅ | ✅ | ✅ | Local |
| MyFreeCams | ✅ | ✅ | ✅ | Local |
| LiveJasmin | ✅ | ✅ | ✅ | Local |
| CamSoda | ✅ | ✅ | ✅ | Local |
| Cams.com | ✅ | ✅ | ✅ | Local |
| Xcams | ✅ | ✅ | ✅ | Local |

New providers expose Discover through provider-specific browse/search pages, then resolve public live HLS/DASH streams through yt-dlp or the integrated Playwright browser. Follow is local unless the provider exposes a compatible authenticated follow API.

## Screenshots

| Discover | Following | Recordings |
|----------|-----------|------------|
| ![Discover](discover.png) | ![Following](following.png) | ![Recordings](recordings.png) |

## Quick Start

### UmbrelOS (one-click install)

P-StreamRec ships as an Umbrel Community App Store. On your Umbrel:

1. Open the **App Store**, click the store menu, then **Community App Stores → Add**
2. Paste the repo URL: `https://github.com/raccommode/P-StreamRec`
3. Install **P-StreamRec** from the store

FlareSolverr is bundled inside the app — no extra setup required. Recordings are stored in `${APP_DATA_DIR}/data` on your Umbrel.

### Docker Compose (recommended, includes FlareSolverr)

```yaml
version: "3.8"
services:
  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    environment:
      - LOG_LEVEL=info
    ports:
      - "8191:8191"
    restart: unless-stopped

  p-streamrec:
    image: ${PSTREAMREC_IMAGE:-ghcr.io/raccommode/p-streamrec:latest}
    depends_on:
      - flaresolverr
    environment:
      - CB_RESOLVER_ENABLED=true
      - FLARESOLVERR_URL=http://flaresolverr:8191
    ports:
      - "8080:8080"
    volumes:
      - ./data:/data
    restart: unless-stopped
```

### Docker Run (simple)

```bash
docker run -d --name p-streamrec \
  -p 8080:8080 -v ./data:/data \
  -e CB_RESOLVER_ENABLED=true \
  ghcr.io/raccommode/p-streamrec:latest
```

**Access:** `http://localhost:8080`

For local image testing without changing the default image:

```bash
docker build -t p-streamrec:local .
PSTREAMREC_IMAGE=p-streamrec:local HOST_PORT=2727 docker compose up -d --force-recreate
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OUTPUT_DIR` | `/data` | Recordings folder |
| `PORT` | `8080` | Web interface port |
| `HOST_PORT` | `8080` | Host port used by Docker Compose |
| `PSTREAMREC_IMAGE` | `ghcr.io/raccommode/p-streamrec:latest` | Docker Compose image override for local builds |
| `FFMPEG_PATH` | `ffmpeg` | Path to FFmpeg |
| `RECORDING_RANGE_CHUNK_SIZE` | `8388608` | Max bytes returned for open-ended replay Range requests |
| `CB_RESOLVER_ENABLED` | `true` | Enable Chaturbate support |
| `PSTREAMREC_DNS_CACHE` | `false` | Enable a container-local DNS cache for FFmpeg/HLS lookups |
| `PSTREAMREC_DNS_CACHE_UPSTREAMS` | — | Optional comma-separated upstream DNS servers for the local DNS cache |
| `PSTREAMREC_BROWSER_HEADLESS` | `true` | Run the integrated Playwright browser headless |
| `PSTREAMREC_BROWSER_CAPTURE_TIMEOUT` | `25` | Seconds to capture HLS/DASH requests from browser-protected providers |
| `CB_REQUEST_DELAY` | `1.0` | Delay between Chaturbate requests (seconds) |
| `PASSWORD` | — | Password to protect the interface (optional) |
| `AUTO_RECORD_USERS` | — | Comma-separated usernames to auto-record |
| `AUTO_RECORD_INTERVAL` | `120` | Default seconds between background live-status checks; can be overridden in Settings |
| `RECORD_SEGMENT_DURATION_MINUTES` | `0` | Optional recording split interval: `0`, `30`, `60`, or `90` minutes |
| `RECORD_SEGMENT_SIZE_MB` | `0` | Optional maximum TS segment size in MB; `0` disables size-based splitting |
| `CHATURBATE_USERNAME` | — | Chaturbate login (optional, enables Following + better quality) |
| `CHATURBATE_PASSWORD` | — | Chaturbate password (optional) |
| `FLARESOLVERR_URL` | — | FlareSolverr URL (e.g. `http://flaresolverr:8191`) |
| `PSTREAMREC_PROXY_URL` | — | Optional outbound proxy for provider requests (`http://`, `https://`, `socks4://`, `socks5://`) |
| `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` | — | Standard proxy env vars, also honored when `PSTREAMREC_PROXY_URL` is unset |
| `NO_PROXY` | `localhost,127.0.0.1,flaresolverr` | Hosts that should bypass standard proxy env vars |
| `TZ` | `UTC` | Timezone (e.g. `America/Toronto`) |

For recording downloads, FFmpeg can use HTTP(S) proxies via `PSTREAMREC_PROXY_URL`
or the standard proxy env vars. SOCKS proxies are supported by the Python
resolvers/API calls; use an HTTP proxy if FFmpeg also needs to fetch HLS
segments through the proxy.

For Chaturbate/CDN recordings that trigger repeated FFmpeg DNS lookups during
live LL-HLS playlist reloads, set `PSTREAMREC_DNS_CACHE=true` to start a small
`dnsmasq` cache inside the P-StreamRec container. The cache keeps repeated
lookups local until the upstream DNS TTL expires. If your container already
uses a localhost resolver, also set `PSTREAMREC_DNS_CACHE_UPSTREAMS=1.1.1.1,1.0.0.1`
or your preferred upstream servers to avoid a resolver loop.

## Usage

1. **Add a model** — click **+**, choose a source, then enter a username or profile URL
2. **Auto-record** — the system checks every 2 minutes and records when live
3. **Auto-convert** — when the stream ends, TS is converted to MP4 automatically
4. **Watch live** — click a model card to open the live player
5. **Browse replays** — go to the Recordings page to watch or delete recordings

### Recording format

- Original: `/data/records/<username>/YYYYMMDD_HHMMSS_ID.ts` (MPEG-TS, lossless)
- Segmented original: `/data/records/<username>/YYYYMMDD_HHMMSS_ID_part001.ts`, `_part002.ts`, ... when duration or size segmentation is enabled
- Converted: `/data/records/<username>/YYYYMMDD_HHMMSS_ID.mp4` (H.264, auto-generated)

### Storage estimates

| Format | Size per hour |
|--------|---------------|
| TS (original) | ~2–4 GB |
| MP4 (converted) | ~600 MB–1.2 GB |

## Development

```bash
git clone https://github.com/raccommode/P-StreamRec.git
cd P-StreamRec
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

**Stack:** FastAPI, SQLite (aiosqlite), HLS.js, FFmpeg, Docker

## License

**Non-Commercial Open Source License** — See [LICENSE](LICENSE)

Free to use, modify, and distribute — **no commercial use** — share modifications under same license — attribution required
