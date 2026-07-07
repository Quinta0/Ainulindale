# 🎵 Ainulindale

> *"There was Eru, the One [...] and he made first the Ainur [...] and they sang before him, and he was glad."*  
> — J.R.R. Tolkien, The Silmarillion

A fully self-hosted, automated music discovery and streaming system.  
No Spotify. No YouTube Music. No ads. Just your music, your server, your rules.

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
| **autoheal** | Watches slskd/gluetun health status and force-restarts them if they hang |
| **Explo** | Discovery engine — connects ListenBrainz recommendations to slskd |
| **ListenBrainz** | Open-source scrobbler + recommendation engine |
| **Last.fm** | Secondary scrobbler for stats and social features |

---

## Prerequisites

- Unraid NAS (or any Linux server with Docker)
- ProtonVPN account (paid, for P2P/WireGuard support)
- Soulseek account — free at [slsknet.org](https://www.slsknet.org)
- ListenBrainz account — free at [listenbrainz.org](https://listenbrainz.org)
- Last.fm account — free at [last.fm](https://www.last.fm)
- Last.fm API key — free at [last.fm/api/account/create](https://www.last.fm/api/account/create)
- Substreamer app on your phone
- Pangolin or any reverse proxy for external access

---

## Folder Structure

```
/mnt/user/appdata/
├── gluetun/               ← VPN state (auto-populated)
├── slskd/
│   └── slskd.yml          ← slskd config (copy from repo)
└── explo/
    ├── .env               ← Explo config (copy from .env.example)
    └── config/            ← Explo's playlist cache + cover art (auto-populated, must persist)

/mnt/user/data/media/music/
├── incomplete/            ← In-progress downloads (Navidrome ignores these)
├── explo/                 ← Explo-managed discovery downloads
└── (your library)/        ← Everything else
```

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/ainulindale
cd ainulindale
```

### 2. Create your config files

```bash
cp .env.example .env
```

Fill in `.env` with your WireGuard private key, paths, and VPN country.

Copy `slskd.yml` to your appdata folder:
```bash
cp slskd.yml /mnt/user/appdata/slskd/slskd.yml
```

Fill in your Soulseek credentials and choose a web UI username/password.

Copy the Explo env block from `.env.example` into a separate file:
```bash
cp .env.example /mnt/user/appdata/explo/.env
```

Fill in your ListenBrainz username, Navidrome URL/credentials, and slskd API key.

### 3. Create required folders

```bash
mkdir -p /mnt/user/appdata/{gluetun,slskd,explo/config}
mkdir -p /mnt/user/data/media/music/{explo,incomplete}
```

### 4. Get your ProtonVPN WireGuard key

1. Log into [account.proton.me](https://account.proton.me) → VPN → Downloads
2. Select **WireGuard** protocol and a **P2P-capable server**
3. Generate the config and copy the `PrivateKey` value into your `.env`

### 5. Start the stack

```bash
# Start VPN first and verify it connects
docker compose up -d gluetun
docker compose logs -f gluetun
# Look for: "Public IP address is x.x.x.x"

# Then start slskd
docker compose up -d slskd
```

Open `http://your-nas-ip:5030`, log in with your slskd credentials, go to **Options → API Keys**, generate a key and paste it into `/mnt/user/appdata/explo/.env` as `SLSKD_API_KEY`.

### 6. Install Navidrome via Unraid Community Apps

Add these environment variables in the Navidrome container template:

| Variable | Value |
|---|---|
| `ND_LASTFM_APIKEY` | your Last.fm API key |
| `ND_LASTFM_SECRET` | your Last.fm shared secret |
| `ND_LASTFM_ENABLED` | `true` |
| `ND_SCANSCHEDULE` | `1m` |

### 7. Start Explo and autoheal

```bash
docker compose up -d explo autoheal
```

Unlike the rest of the stack, Explo doesn't need any external scheduler (no Unraid User Scripts, no cron). It runs continuously and schedules Weekly Exploration and Daily Jams itself via `WEEKLY_EXPLORATION_SCHEDULE` / `DAILY_JAMS_SCHEDULE` in `docker-compose.yml`, and `EXECUTE_ON_START=false` means restarting or updating the container never triggers an extra, out-of-schedule run. This is what keeps it from downloading the same week twice — see [Troubleshooting](#troubleshooting) below for the full explanation.

`autoheal` also just runs continuously in the background; it watches slskd and gluetun and restarts either one automatically if Docker marks it unhealthy.

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

## Troubleshooting

### slskd hangs and needs a manual restart every ~24h

This was almost always one of two things, both addressed in the current compose file:

- **slskd itself getting stuck** (memory pressure / stuck transfers) — fixed by pinning `slskd/slskd:0.25.1`, which bundles a round of upstream fixes for stuck and failing transfers.
- **gluetun's tunnel silently hanging** — since slskd runs inside gluetun's network namespace, a frozen gluetun looks identical to a frozen slskd. Fixed by pinning `qmcgaw/gluetun:v3.41.1`, which resolves a healthcheck race condition that could make gluetun hang completely.

Both containers already ship a Docker healthcheck, but **Docker does not restart a container just because it's unhealthy** — that gap is why a manual restart was needed. The new `autoheal` service closes it: it watches both containers (via the `autoheal=true` label) and force-restarts whichever one goes unhealthy, with no manual intervention.

If slskd still misbehaves after this, check `docker ps` for its health status and `docker logs slskd`/`docker logs gluetun` before restarting manually.

### Weekly Exploration downloaded multiple times

The old setup ran two one-shot `explo-weekly` / `explo-daily` containers (`restart: "no"`, `EXECUTE_ON_START=true`) that were triggered by an external Unraid User Scripts cron. That design had two compounding problems:

1. Every container restart (a host reboot, a Docker update, the scheduler firing slightly out of sync) re-ran Explo immediately because of `EXECUTE_ON_START=true`, regardless of whether that week's playlist already existed.
2. Explo's playlist cache had nowhere to persist between runs (no `/opt/explo/config` volume), so each fresh container had no memory of "I already built this week's playlist" and would build it again.

The current compose runs a single, always-on `explo` container with its own internal cron (`WEEKLY_EXPLORATION_SCHEDULE` / `DAILY_JAMS_SCHEDULE`), `EXECUTE_ON_START=false`, and a persistent `/opt/explo/config` volume, plus the newer `v1.1.0` image which includes further upstream fixes for scheduled-job bugs. Together this means Explo only ever runs on its own schedule, and remembers what it already did across restarts.

**One-time cleanup:** this fix prevents *future* duplicates, but it won't retroactively merge the duplicate `Weekly-Exploration-2026-WeekXX` playlists already sitting in Navidrome. Delete the extras once, manually, from the Navidrome UI (or via its API) — new runs going forward should stay to one playlist per period.

---

## Tips

- **ListenBrainz needs time** — it takes a few weeks of scrobbling before Weekly Exploration playlists are generated. Speed this up by importing your Google Takeout YouTube Music watch history via [ytm-extractor](https://community.metabrainz.org/t/ytm-extractor-import-youtube-music-listens-to-listenbrainz/707619).
- **Manual downloads** — search for any artist or album directly in the slskd web UI at `http://your-nas-ip:5030`. Downloads land straight in your Navidrome library.
- **Playlist import** — use [Soundiiz](https://soundiiz.com) to transfer playlists from YouTube Music/Spotify directly into Navidrome. Tracks already in your library get matched automatically.
- **Quality** — slskd is tried first for every download. YouTube is only used as a last resort fallback for tracks not found on Soulseek.
- **Sharing** — slskd shares your music library back to the Soulseek network. The more you share, the better your download priority from other peers.
- **ProtonVPN** — the WireGuard private key in Gluetun pins you to a specific server. Make sure to use a P2P-capable server when generating your WireGuard config on ProtonVPN's site.

---

## License

MIT — do whatever you want with it.