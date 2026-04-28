# Ainulindale
A fully self-hosted, automated music discovery and streaming system. No Spotify. No YouTube Music. No ads. Just your music, your server, your rules.

---

## What This Stack Does

- **Streams** your music library to any device via a Spotify-like interface
- **Automatically discovers** new music weekly based on your listening habits
- **Downloads** discovered tracks at high quality (FLAC/WAV preferred, 320kbps minimum) from the Soulseek network
- **Scrobbles** everything you listen to Last.fm and ListenBrainz
- **Routes all P2P traffic through a VPN** so your real IP is never exposed

---

## Components

| Component | Role |
|---|---|
| **Navidrome** | Music server — indexes your library, streams to clients |
| **Substreamer** | Mobile app (iOS/Android) — plays music from Navidrome |
| **Pangolin** | Reverse proxy — exposes Navidrome securely to the internet |
| **Gluetun** | VPN gateway container — all Soulseek traffic exits via ProtonVPN |
| **slskd** | Soulseek client — searches and downloads music from the P2P network |
| **Explo** | Discovery engine — connects ListenBrainz recommendations to slskd |
| **ListenBrainz** | Open-source scrobbler + recommendation engine |
| **Last.fm** | Secondary scrobbler for stats and social features |

---

## Folder Structure

```
/mnt/user/appdata/
├── gluetun/               ← VPN state (auto-populated)
├── slskd/
│   └── slskd.yml          ← slskd config
└── explo/
    ├── .env               ← Explo config
    └── docker-compose.yml ← Full stack compose file

/mnt/user/data/media/music/
├── incomplete/            ← In-progress downloads (Navidrome ignores these)
├── explo/                 ← Explo-managed discovery downloads
└── (your library)/        ← Everything else
```

---

## docker-compose.yml

```yaml
services:

  gluetun:
    image: qmcgaw/gluetun:latest
    container_name: gluetun
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    environment:
      - VPN_SERVICE_PROVIDER=protonvpn
      - VPN_TYPE=wireguard
      - WIREGUARD_PRIVATE_KEY=YOUR_PRIVATE_KEY_HERE
      - WIREGUARD_ADDRESSES=10.2.0.2/32
      - SERVER_COUNTRIES=Switzerland
      - TZ=Europe/Zurich
    ports:
      - 5030:5030
    volumes:
      - /mnt/user/appdata/gluetun:/gluetun
    restart: unless-stopped

  slskd:
    image: slskd/slskd:latest
    container_name: slskd
    network_mode: "service:gluetun"
    depends_on:
      gluetun:
        condition: service_healthy
    environment:
      - SLSKD_REMOTE_CONFIGURATION=true
    volumes:
      - /mnt/user/appdata/slskd:/app
      - /mnt/user/data/media/music:/downloads
      - /mnt/user/data/media/music:/music:ro
    restart: unless-stopped

  explo-weekly:
    image: ghcr.io/lumepart/explo:latest
    container_name: explo-weekly
    environment:
      - EXECUTE_ON_START=true
      - WEEKLY_EXPLORATION_FLAGS=--playlist=weekly-exploration --persist
    volumes:
      - /mnt/user/appdata/explo/.env:/opt/explo/.env
      - /mnt/user/data/media/music/explo:/data
      - /mnt/user/data/media/music:/slskd
    restart: "no"

  explo-daily:
    image: ghcr.io/lumepart/explo:latest
    container_name: explo-daily
    environment:
      - EXECUTE_ON_START=true
      - DAILY_JAMS_FLAGS=--playlist=daily-jams --download-mode=skip
    volumes:
      - /mnt/user/appdata/explo/.env:/opt/explo/.env
      - /mnt/user/data/media/music/explo:/data
      - /mnt/user/data/media/music:/slskd
    restart: "no"
```

---

## Explo .env

```env
# --- Discovery ---
DISCOVERY_SERVICE=listenbrainz
LISTENBRAINZ_USER=your_listenbrainz_username
LISTENBRAINZ_DISCOVERY=playlist

# --- Navidrome (Subsonic API) ---
EXPLO_SYSTEM=subsonic
SYSTEM_URL=http://192.168.1.x:4533
SYSTEM_USERNAME=your_navidrome_username
SYSTEM_PASSWORD=your_navidrome_password

# --- Downloaders (slskd first, YouTube fallback) ---
DOWNLOAD_SERVICES=slskd,youtube
TRACK_EXTENSION=mp3

# --- slskd ---
SLSKD_URL=http://slskd:5030
SLSKD_API_KEY=your_slskd_api_key
MIGRATE_DOWNLOADS=true
RENAME_TRACK=true

# --- Quality gates (FLAC/WAV preferred, 320kbps minimum) ---
EXTENSIONS=flac,wav,mp3
MIN_BITRATE=320
MIN_BIT_DEPTH=16

# --- YouTube fallback (optional) ---
YOUTUBE_API_KEY=your_youtube_api_key

# --- Misc ---
SLEEP=2
LOG_LEVEL=INFO
```

---

## slskd.yml

```yaml
soulseek:
  username: your_soulseek_username
  password: your_soulseek_password

web:
  authentication:
    username: your_chosen_web_username
    password: your_chosen_web_password
    api_keys:
      explo_key:
        key: your_api_key_minimum_16_chars_long
        role: readwrite
        cidr: 0.0.0.0/0,::/0

directories:
  incomplete: /downloads/incomplete
  downloads: /downloads

transfers:
  download:
    slots: 50
  upload:
    slots: 20

shares:
  directories:
    - /music
```

---

## Navidrome (Unraid Community Apps)

Navidrome is installed via Unraid's Community Apps store. Add these extra environment variables in the container template:

| Variable | Value |
|---|---|
| `ND_LASTFM_APIKEY` | your Last.fm API key |
| `ND_LASTFM_SECRET` | your Last.fm shared secret |
| `ND_LASTFM_ENABLED` | `true` |
| `ND_SCANSCHEDULE` | `1m` |

Last.fm API credentials: [last.fm/api/account/create](https://www.last.fm/api/account/create)

---

## Automation — Unraid User Scripts

Install the **User Scripts** plugin from Community Apps, then add two scripts:

### Weekly Exploration (every Tuesday at 00:15)
Cron: `15 0 * * 2`
```bash
#!/bin/bash
docker compose -f /mnt/user/appdata/explo/docker-compose.yml up explo-weekly
```

### Daily Jams (every day at 00:15)
Cron: `15 0 * * *`
```bash
#!/bin/bash
docker compose -f /mnt/user/appdata/explo/docker-compose.yml up explo-daily
```

---

## How Discovery Works

```
You listen in Substreamer
        ↓
Navidrome scrobbles to Last.fm + ListenBrainz
        ↓
ListenBrainz builds your taste profile over time
        ↓
Every Monday night: ListenBrainz generates Weekly Exploration playlist
        ↓
Tuesday 00:15: Explo pulls recommendations
        ↓
slskd searches Soulseek network (behind ProtonVPN)
        ↓
Downloads FLAC/WAV/320kbps+ to /mnt/user/data/media/music
        ↓
Navidrome scans every 1 minute → appears in Substreamer
        ↓
You discover new music 🎵
```

---

## Tips

- **ListenBrainz needs time** — it takes a few weeks of scrobbling before Weekly Exploration playlists are generated. Speed this up by importing your Google Takeout YouTube Music watch history via [ytm-extractor](https://community.metabrainz.org/t/ytm-extractor-import-youtube-music-listens-to-listenbrainz/707619).
- **Manual downloads** — search for any artist or album directly in the slskd web UI at `http://your-nas-ip:5030`. Downloads land straight in your Navidrome library.
- **Playlist import** — use [Soundiiz](https://soundiiz.com) to transfer playlists from YouTube Music/Spotify directly into Navidrome. Tracks already in your library get matched automatically.
- **Quality** — slskd is tried first for every download. YouTube is only used as a last resort fallback for tracks not found on Soulseek.
- **Sharing** — slskd shares your music library back to the Soulseek network. The more you share, the better your download priority from other peers.
- **ProtonVPN** — the WireGuard private key in Gluetun pins you to a specific server. Make sure to use a P2P-capable server when generating your WireGuard config on ProtonVPN's site.

---

## Prerequisites

- Unraid NAS (or any Linux server with Docker)
- ProtonVPN account (paid, for P2P/WireGuard support)
- Soulseek account — free at [slsknet.org](https://www.slsknet.org)
- ListenBrainz account — free at [listenbrainz.org](https://listenbrainz.org)
- Last.fm account — free at [last.fm](https://www.last.fm)
- Last.fm API key — free at [last.fm/api](https://www.last.fm/api/account/create)
- Substreamer app on your phone
- Pangolin or any reverse proxy for external access
