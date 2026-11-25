# 🎵 Ainulindale

> *"There was Eru, the One [...] and he made first the Ainur [...] and they sang before him, and he was glad."*  
> — J.R.R. Tolkien, The Silmarillion

A fully self-hosted, automated music discovery and streaming system.  
No Spotify. No YouTube Music. No ads. Just your music, your server, your rules.

**Site:** [quinta0.github.io/Ainulindale](https://quinta0.github.io/Ainulindale/) · **Image:** `ghcr.io/quinta0/ainulindale`

---

## What This Stack Does

- **Streams** your library to any device through Navidrome
- **Discovers** new music every week from **two independent sources**: ListenBrainz Weekly Exploration and Last.fm recommendations. If one of them is down, the other still delivers
- **Downloads** the discoveries from Soulseek at high quality (FLAC/WAV preferred, 320 kbps minimum), through a VPN
- **Keeps only this week and last week** of discoveries. Older playlists and their files are removed, except tracks you starred or added to a playlist of your own
- **Heals itself**: a Soulseek session that drops is reconnected, and a stuck slskd is restarted, without you noticing
- **One container.** `ghcr.io/quinta0/ainulindale` runs the VPN, slskd and the discovery schedule together, and downloads slskd and Explo itself on first start

## Components

Everything except Navidrome runs in **one container**:

| Inside the container | Role |
|---|---|
| **gluetun** | The base image. VPN tunnel + kill switch; all traffic exits via ProtonVPN |
| **slskd** | Soulseek client, downloaded on first start (`SLSKD_VERSION`), watched and restarted by `start.sh` |
| **Explo** | Searches slskd, downloads, builds the Navidrome playlist (downloaded, `EXPLO_VERSION`) |
| **discover.py** | Decides *what* Explo imports and *when*; adds Last.fm, outage handling and retention |
| **yt-dlp** | YouTube fallback, updated every night |

**Navidrome** is installed separately (e.g. Unraid Community Apps).

Updating slskd or Explo is a version bump in `.env`/compose and a restart; the new binary is fetched through the VPN, the old one is deleted. Updating the image (`docker compose pull`) brings a new gluetun and new scripts.

## How Discovery Works

Once a day (`DISCOVERY_SCHEDULE`, default 00:15) the orchestrator runs a **tick**. A tick only does work that is still missing, so it is safe at any frequency and after any restart:

```
tick
 ├─ ListenBrainz: is there a Weekly Exploration playlist I have not imported yet?
 │     yes → check slskd is logged in → Explo imports it → "Weekly-Exploration-2026-Week38"
 │     no  → nothing to do            LB down → note it, try again next tick
 ├─ Last.fm: does this week's playlist exist yet?
 │     no  → pull recommendations → hand them to Explo → "Lastfm-Recommended-2026-Week38"
 └─ Prune: keep the newest KEEP_WEEKS per source, remove the rest
```

Things worth knowing:

- **ListenBrainz is tracked by playlist ID, not by calendar.** If ListenBrainz publishes late, or is down on Tuesday, the playlist is simply picked up on the first tick after it appears. If it never appears, last week's playlist is *not* re-imported under a new name. A playlist that keeps failing is attempted 3 times, then left alone.
- **Last.fm recommendations** come from the same feed the Last.fm website player uses (`last.fm/player/station/user/<you>/recommended`). It is public, needs no login, and returns Last.fm's own picks of artists you have not played. It is not an official API, so with `LASTFM_API_KEY` set there is a second path built purely on the official API (artists similar to your last three months, minus everyone you already know). Tracks recommended in earlier weeks are remembered and not repeated.
- **`LASTFM_MODE`**: `always` gives you two playlists a week; `fallback` only steps in, from Wednesday on, in weeks where ListenBrainz has produced nothing; `off` disables it.
- **slskd preflight.** Before Explo starts, the orchestrator checks that slskd is really logged in to Soulseek and nudges it if not. If it stays down, the run is postponed to the next tick. Without this, one bad night means fifty YouTube rips instead of fifty FLACs.
- **No jams.** Weekly Jams and Daily Jams are gone. `PRUNE_LEGACY_JAMS=true` cleans up what the old setup left behind.

### Retention

With `KEEP_WEEKS=2` you always have this week's and last week's playlist per source. On each tick, anything older goes: the Navidrome playlist and the matching folder under `music/explo/`.

Before a folder is deleted, every file in it is checked against your **starred tracks and every playlist that is staying**. Matches are moved to `music/explo/Keepers/` instead of deleted, so starring a track is how you say "keep this one". Only folders the pipeline created (`Weekly-Exploration-*`, `Custom-Lastfm-*`) are ever touched.

Preview it first:

```bash
docker exec ainulindale python3 /ainulindale/discover.py prune --dry-run
```

`PRUNE_FILES=false` removes old playlists but leaves all files on disk.

## Why slskd Stays Connected

1. **The health check looks at Soulseek, not at the web server.** Every minute `slskd/healthcheck.sh` asks slskd whether it is logged in. On a drop it first asks slskd to reconnect; only three failed checks in a row restart slskd. This used to be the job of the separate autoheal container.
2. **No more stranded network namespace.** slskd used to borrow gluetun's network from a second container, and a gluetun restart left it holding a dead one. In one container there is only one network; if gluetun exits, the whole container exits and Docker's restart policy brings everything back together.
3. **slskd knows about the VPN.** Its native gluetun integration drops the Soulseek session when the tunnel drops, logs in again when it is back, and applies the forwarded port by itself when port forwarding is on.

When the tunnel itself is down, the check stays green on purpose: restarting slskd cannot fix a VPN, and gluetun repairs its own tunnel.

One thing no check can fix: **do not log in with the same Soulseek account anywhere else** (Nicotine+, SoulseekQt on a desktop). The server kicks the older session, and the two clients will keep throwing each other out.

---

## Setup

### 1. Two files

```bash
mkdir -p /mnt/user/appdata/Ainulindale && cd /mnt/user/appdata/Ainulindale
wget https://raw.githubusercontent.com/Quinta0/Ainulindale/main/docker-compose.yml
wget -O .env https://raw.githubusercontent.com/Quinta0/Ainulindale/main/.env.example
```

Fill in `.env`: VPN key, Soulseek login, Navidrome login, ListenBrainz and Last.fm names. The internal API keys are generated for you. Give `NAVIDROME_URL` as an **IP address**: all traffic goes through the VPN's firewall, which is opened for exactly that address.

### 2. Folders

```bash
MUSIC_PATH=/mnt/user/data/media/music    # same as in .env
mkdir -p $MUSIC_PATH/{explo,slskd/incomplete}
touch $MUSIC_PATH/slskd/incomplete/.ndignore     # Navidrome skips half-downloaded files
```

```
./config/                      ← created on first start
├── gluetun/
├── slskd/slskd.yml            ← tuning only, no secrets (seeded on first start)
├── explo/                     ← Explo cache + ainulindale-state.json (must persist)
└── bin/                       ← downloaded slskd, Explo, yt-dlp

$MUSIC_PATH/
├── slskd/                     ← slskd downloads (manual ones show up in Navidrome from here)
│   └── incomplete/
├── explo/
│   ├── Weekly-Exploration-2026-Week38/
│   ├── Custom-Lastfm-2026-38/
│   └── Keepers/               ← rescued favourites
└── (your library)
```

### 3. ProtonVPN key

[account.proton.me](https://account.proton.me) → VPN → Downloads → WireGuard, pick a P2P server, copy `PrivateKey` into `.env`.

### 4. Navidrome

| Variable | Value |
|---|---|
| `ND_LASTFM_ENABLED` | `true` |
| `ND_LASTFM_APIKEY` / `ND_LASTFM_SECRET` | your Last.fm API key and secret |
| `ND_SCANSCHEDULE` | `1m` |

Link both Last.fm and ListenBrainz in Navidrome's user settings so both services learn from what you play.

### 5. Start and verify

```bash
docker compose up -d
docker logs -f ainulindale                # VPN is up → downloading slskd → discovery tick scheduled
docker exec ainulindale python3 /ainulindale/discover.py status
docker exec ainulindale python3 /ainulindale/discover.py lastfm-preview   # what Last.fm would give you
docker exec ainulindale python3 /ainulindale/discover.py run              # a full tick now, instead of waiting for 00:15
```

## Migrating From the Multi-Container Stack

The state of the old gluetun, slskd and explo containers moves into one folder; nothing is re-downloaded or re-imported.

```bash
cd /mnt/user/appdata/Ainulindale
docker compose down                       # stops gluetun, slskd, explo, autoheal (and lidarr, see below)
APPDATA_PATH=/mnt/user/appdata            # as in your old .env
mkdir -p config
mv $APPDATA_PATH/gluetun      config/gluetun
mv $APPDATA_PATH/slskd        config/slskd
mv $APPDATA_PATH/explo/config config/explo
git pull                                  # or fetch the new docker-compose.yml
docker compose up -d
```

- `.env` keeps working as is. `GLUETUN_API_KEY` is no longer needed; `VPN_PORT_FORWARDING` is still understood, `PORT_FORWARDING` is the new name.
- If `NAVIDROME_URL` used a hostname, change it to the IP (or set `FIREWALL_OUTBOUND_SUBNETS`).
- `SLSKD_DOWNLOADS_PATH` is gone: slskd downloads to `$MUSIC_PATH/slskd`.
- **Lidarr** is not part of Ainulindale any more. If you use it, install it from Community Apps (`lscr.io/linuxserver/lidarr`, `/config` → `appdata/lidarr`, `/music` → your library) and its data is picked up where it was.
- `discovery/` and `slskd/healthcheck.sh` are inside the image now; the checked-out copies are no longer mounted.

## Troubleshooting

**slskd keeps restarting.** `docker logs ainulindale | grep 'slskd check failed'` shows what the check saw. "cannot read Soulseek state" means `SLSKD_API_KEY` is not the key slskd is using (old `slskd.yml` still in place?). "logged out of Soulseek" every time, with the log mentioning another client, means the account is in use elsewhere.

**slskd exits right after start.** It validates its settings and exits on the first bad one; `docker logs ainulindale --tail 60` names it. Usual causes: a missing `music/slskd/incomplete` folder, an `SLSKD_API_KEY` under 16 characters.

**slskd never logs in, log says it is waiting for the VPN.** `PORT_FORWARDING=true` on a Proton plan/server without port forwarding.

**Explo cannot reach Navidrome.** The VPN firewall blocks the LAN except `NAVIDROME_URL`'s IP. With a hostname, set `FIREWALL_OUTBOUND_SUBNETS=192.168.1.0/24` (your LAN).

**Tick says "slskd did not log in ... Skipping this run".** Working as intended; it retries on the next tick. `REQUIRE_SLSKD=false` lets runs go ahead on YouTube alone.

**Last.fm gives 0 tracks.** Open `https://www.last.fm/player/station/user/<you>/recommended` in a browser. Empty JSON means the account has too few scrobbles, or the profile's listening data is private. Set `LASTFM_API_KEY` to enable the API-based path.

**Prune says it could not remove a folder.** Files from the old root-owned setup while Explo now runs with `PUID`/`PGID`. `chown -R` the `music/explo` folder.

**Upgrading Explo.** The Last.fm bridge writes Explo's playlist cache (`config/cache/custom-*.json`), an internal format. It is pinned with `EXPLO_VERSION=v1.2.0`; after bumping, run `discover.py run` once and watch the log. If Explo ever ships Last.fm support itself ([issue #135](https://github.com/LumePart/Explo/issues/135)), the bridge can retire.

## Tips

- **ListenBrainz needs a few weeks** of scrobbles before Weekly Exploration appears. Last.fm recommendations work with whatever history your account already has, which makes it a good bridge in the meantime.
- **Manual downloads**: search in the slskd UI at `http://your-nas-ip:5030`; results land in `music/slskd/`.
- **Sharing**: slskd shares your library back. The more you share, the better your queue position with other peers.
- **Port forwarding** noticeably improves Soulseek results. If your Proton plan has it, set `PORT_FORWARDING=true` (one switch for gluetun and slskd).

---

## License

MIT. Do whatever you want with it.
