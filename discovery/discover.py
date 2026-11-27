#!/usr/bin/env python3
"""
Ainulindale discovery orchestrator.

Runs inside the Explo container (python 3.12, stdlib only) and drives the Explo
binary instead of Explo's built-in cron. One idempotent "tick" does this:

  1. Preflight   make sure slskd is actually logged in to Soulseek, otherwise
                 Explo would silently fall back to YouTube for the whole week.
  2. ListenBrainz  import Weekly Exploration, but only if ListenBrainz has
                 published a playlist we have not imported yet.
  3. Mixes       build our own daily / weekly / monthly discovery playlists from
                 ListenBrainz data (similar artists, popularity) and hand them to
                 Explo as "custom" playlists. Lookups are cached in SQLite.
  4. Prune       keep the newest N playlists per source, remove older ones
                 and their files; starred / playlisted tracks are rescued.

Because every step checks state first, the tick can run as often as you like
and after any restart without ever producing duplicates.

Usage:  discover.py run | status | prune [--dry-run] | preview [daily|weekly|monthly]
"""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------
# Configuration (all from environment, see .env.example)
# --------------------------------------------------------------------------


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool) -> bool:
    v = env(name)
    return default if v == "" else v.lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


CONFIG_DIR = env("WEB_DATA_PATH", "/opt/explo/config")  # Explo reads its playlist cache from here
DATA_DIR = env("DOWNLOAD_DIR", "/data")
SLSKD_DIR = env("SLSKD_DIR", "/slskd")
EXPLO_BIN = env("EXPLO_BIN", "/opt/explo/explo")
EXPLO_ENV = env("WEB_ENV_PATH", "/opt/explo/.env")
STATE_FILE = os.path.join(CONFIG_DIR, "ainulindale-state.json")
LOCK_FILE = os.path.join(CONFIG_DIR, "ainulindale.lock")
KEEPERS_DIR = os.path.join(DATA_DIR, "Keepers")

LB_USER = env("LISTENBRAINZ_USER")
LB_TOKEN = env("LISTENBRAINZ_USER_TOKEN")
LB_ENABLED = env_bool("LISTENBRAINZ_ENABLED", True)
LB_MAX_ATTEMPTS = env_int("LISTENBRAINZ_MAX_ATTEMPTS", 3)

DB_FILE = os.path.join(CONFIG_DIR, "ainulindale.db")  # ListenBrainz/MusicBrainz cache + pick history
DISCOVERY_MIXES = env("DISCOVERY_MIXES", "weekly")  # e.g. daily,weekly:40,monthly
AVOID_TAGS = {t.strip().lower() for t in env("DISCOVERY_AVOID_TAGS").split(",") if t.strip()}
PREFER_TAGS = {t.strip().lower() for t in env("DISCOVERY_PREFER_TAGS").split(",") if t.strip()}
ARTIST_COOLDOWN_DAYS = env_int("DISCOVERY_ARTIST_COOLDOWN_DAYS", 90)  # a recommended artist rests this long

NAVIDROME_URL = env("SYSTEM_URL").rstrip("/")
NAVIDROME_USER = env("SYSTEM_USERNAME")
NAVIDROME_PASS = env("SYSTEM_PASSWORD")

SLSKD_URL = env("SLSKD_URL").rstrip("/")
SLSKD_API_KEY = env("SLSKD_API_KEY")
USES_SLSKD = "slskd" in env("DOWNLOAD_SERVICES", "slskd,youtube").split(",")
REQUIRE_SLSKD = env_bool("REQUIRE_SLSKD", True)
SLSKD_WAIT_MIN = env_int("SLSKD_WAIT_MINUTES", 10)

KEEP_WEEKS = max(1, env_int("KEEP_WEEKS", 2))
PRUNE_FILES = env_bool("PRUNE_FILES", True)
KEEP_FAVOURITES = env_bool("KEEP_FAVOURITES", True)
PRUNE_LEGACY_JAMS = env_bool("PRUNE_LEGACY_JAMS", False)
EXPLO_TIMEOUT_MIN = env_int("EXPLO_TIMEOUT_MINUTES", 480)

USER_AGENT = "Ainulindale/2.1 ( https://github.com/Quinta0/Ainulindale )"  # the format LB and MB ask for
AUDIO_EXT = {".flac", ".wav", ".mp3", ".m4a", ".ogg", ".opus", ".aac", ".wma", ".aiff", ".ape"}


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} [ainulindale] {msg}", flush=True)


# --------------------------------------------------------------------------
# Small HTTP helper
# --------------------------------------------------------------------------


class HttpError(Exception):
    pass


def http_json(url, *, method="GET", headers=None, params=None, timeout=20, retries=2, allow_empty=False):
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    hdrs.update(headers or {})
    last = None
    for attempt in range(retries + 1):
        try:
            data = b"" if method in ("PUT", "POST") else None
            req = urllib.request.Request(url, method=method, headers=hdrs, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            if not body.strip():
                if allow_empty:
                    return None
                raise HttpError("empty response")
            return json.loads(body)
        except urllib.error.HTTPError as e:
            last = HttpError(f"HTTP {e.code} from {urllib.parse.urlsplit(url).netloc}")
            if e.code in (400, 401, 403, 404):
                break
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = HttpError(f"{type(e).__name__}: {e}")
        if attempt < retries:
            time.sleep(3 * (attempt + 1))
    raise last


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    state.setdefault("listenbrainz", {})
    state.setdefault("mixes", {})
    state.pop("lastfm", None)  # pre-2.1 state
    return state


def save_state(state: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def iso_week(d: dt.date | None = None) -> tuple[int, int]:
    y, w, _ = (d or dt.date.today()).isocalendar()
    return y, w


def week_key(d: dt.date | None = None) -> str:
    y, w = iso_week(d)
    return f"{y}-W{w:02d}"


# --------------------------------------------------------------------------
# slskd
# --------------------------------------------------------------------------


class Slskd:
    def __init__(self, url: str, key: str):
        self.url, self.key = url, key

    def server_state(self) -> dict:
        return http_json(f"{self.url}/api/v0/server", headers={"X-API-Key": self.key}, timeout=10, retries=0)

    def connect(self) -> None:
        http_json(f"{self.url}/api/v0/server", method="PUT", headers={"X-API-Key": self.key},
                  timeout=10, retries=0, allow_empty=True)

    def wait_until_logged_in(self, minutes: int) -> bool:
        """True once slskd reports Connected+LoggedIn. Nudges a reconnect while waiting."""
        deadline = time.monotonic() + minutes * 60
        nudged_at = None  # not 0.0: monotonic() itself can be < 120 right after a host boot
        while True:
            try:
                st = self.server_state()
                if st.get("isConnected") and st.get("isLoggedIn"):
                    return True
                desc = st.get("state", "unknown")
                transitioning = st.get("isConnecting") or st.get("isLoggingIn")
                if not transitioning and (nudged_at is None or time.monotonic() - nudged_at > 120):
                    log(f"slskd is '{desc}', asking it to reconnect")
                    try:
                        self.connect()
                    except HttpError as e:
                        log(f"reconnect request failed: {e}")
                    nudged_at = time.monotonic()
            except HttpError as e:
                log(f"slskd not reachable at {self.url}: {e}")
            if time.monotonic() >= deadline:
                return False
            time.sleep(20)


def slskd_ready() -> bool:
    if not USES_SLSKD:
        return True
    if not (SLSKD_URL and SLSKD_API_KEY):
        log("SLSKD_URL / SLSKD_API_KEY not set, skipping slskd preflight")
        return True
    if Slskd(SLSKD_URL, SLSKD_API_KEY).wait_until_logged_in(SLSKD_WAIT_MIN):
        return True
    if REQUIRE_SLSKD:
        log(f"slskd did not log in to Soulseek within {SLSKD_WAIT_MIN} min. Skipping this run so the week "
            "is not filled with YouTube rips; the next tick will retry.")
        return False
    log("slskd is not logged in, continuing anyway because REQUIRE_SLSKD=false")
    return True


# --------------------------------------------------------------------------
# ListenBrainz
# --------------------------------------------------------------------------

JSPF_PLAYLIST = "https://musicbrainz.org/doc/jspf#playlist"


def lb_latest_playlist(user: str, patch: str = "weekly-exploration") -> dict | None:
    """Newest 'created for you' playlist of the given type: {'mbid', 'date', 'title'} or None."""
    headers = {"Authorization": f"Token {LB_TOKEN}"} if LB_TOKEN else {}
    best = None
    offset = 0
    while True:
        page = http_json(
            f"https://api.listenbrainz.org/1/user/{urllib.parse.quote(user)}/playlists/createdfor",
            params={"count": 50, "offset": offset}, headers=headers, timeout=30)
        items = page.get("playlists", [])
        for item in items:
            pl = item.get("playlist", {})
            meta = pl.get("extension", {}).get(JSPF_PLAYLIST, {}).get("additional_metadata", {})
            if meta.get("algorithm_metadata", {}).get("source_patch") != patch:
                continue
            date = pl.get("date", "")
            if best is None or date > best["date"]:
                best = {"mbid": pl.get("identifier", "").rstrip("/").rsplit("/", 1)[-1],
                        "date": date, "title": pl.get("title", "")}
        offset += len(items)
        if not items or offset >= page.get("playlist_count", 0):
            return best


def lb_is_fresh(state: dict) -> bool:
    """Has a ListenBrainz playlist published in the last 7 days been imported?"""
    date = state["listenbrainz"].get("imported_date", "")
    try:
        published = dt.datetime.fromisoformat(date.replace("Z", "+00:00"))
    except ValueError:
        return False
    return dt.datetime.now(dt.timezone.utc) - published < dt.timedelta(days=7)


# --------------------------------------------------------------------------
# Discovery mixes: our own recommendations, built from ListenBrainz data
# --------------------------------------------------------------------------
#
# seeds (artists you play) -> similar artists, hop after hop -> minus artists you
# already know -> one track each (+ whole albums on the slower mixes).
# The slower the mix, the further it walks from your seeds and the less obvious
# the tracks it takes: daily stays close, monthly wanders.

LB_API = "https://api.listenbrainz.org/1"
LB_SIMILAR = "https://labs.api.listenbrainz.org/similar-artists/json"
LB_SIMILAR_ALGO = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
MB_API = "https://musicbrainz.org/ws/2"


class Mix:
    def __init__(self, name, period, tracks, seed_range, seeds, hops, rank, albums):
        self.name, self.period, self.tracks = name, period, tracks
        self.seed_range, self.seeds = seed_range, seeds  # LB stats range, how many seed artists to draw
        self.hops = hops      # (min, max) distance from your seeds a pick may come from
        self.rank = rank      # slice of an artist's most played tracks to pick from: (0, 3) = the hits
        self.albums = albums  # whole albums added on top of the single tracks

    def slot(self, d: dt.date | None = None) -> tuple[int, int, str]:
        d = d or dt.date.today()
        if self.period == "day":
            return d.year, d.timetuple().tm_yday, "Day"
        if self.period == "month":
            return d.year, d.month, "Month"
        return (*iso_week(d), "Week")

    def ids(self, d: dt.date | None = None) -> tuple[str, str]:
        y, n, unit = self.slot(d)
        # digits only after the prefix: Explo title-cases the id to build the download folder name
        return f"custom-{self.name}-{y}-{n}", f"Discovery-{self.name.title()}-{y}-{unit}{n}"


PRESETS = {
    "daily": Mix("daily", "day", 15, "week", 8, (1, 1), (0, 3), 0),
    "weekly": Mix("weekly", "week", 30, "month", 12, (1, 2), (0, 10), 1),
    "monthly": Mix("monthly", "month", 40, "half_yearly", 15, (2, 3), (3, 25), 2),
}


def configured_mixes() -> list[Mix]:
    out = []
    for item in DISCOVERY_MIXES.lower().split(","):
        name, _, n = item.strip().partition(":")
        if name in ("", "off", "none"):
            continue
        if name not in PRESETS:
            log(f"DISCOVERY_MIXES: unknown mix '{name}' (daily, weekly, monthly)")
            continue
        p = PRESETS[name]
        out.append(Mix(p.name, p.period, int(n) if n.isdigit() else p.tracks, p.seed_range, p.seeds,
                       p.hops, p.rank, p.albums))
    return out


def track_key(artist: str, title: str) -> str:
    return re.sub(r"[^\w]+", "", f"{artist}|{title}".lower(), flags=re.UNICODE)


_db = None


def db():
    global _db
    if _db is None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        _db = sqlite3.connect(DB_FILE, timeout=30)
        _db.executescript("""
            CREATE TABLE IF NOT EXISTS cache (url TEXT PRIMARY KEY, at REAL, body TEXT);
            CREATE TABLE IF NOT EXISTS picks (kind TEXT, id TEXT, at REAL, PRIMARY KEY (kind, id));
        """)
        _db.execute("DELETE FROM cache WHERE at < ?", (time.time() - 365 * 86400,))
        _db.commit()
    return _db


def cached(url: str, ttl_days: float, **params):
    """GET with a SQLite cache. A stale copy beats nothing when the service is down."""
    key = f"{url}?{urllib.parse.urlencode(sorted(params.items()))}"
    row = db().execute("SELECT at, body FROM cache WHERE url = ?", (key,)).fetchone()
    if row and time.time() - row[0] < ttl_days * 86400:
        return json.loads(row[1])
    headers = {"Authorization": f"Token {LB_TOKEN}"} if LB_TOKEN and "listenbrainz.org" in url else {}
    try:
        data = http_json(url, params=params, headers=headers, timeout=30, allow_empty=True)
    except HttpError:
        if row:
            return json.loads(row[1])
        raise
    finally:
        time.sleep(1)  # LB and MB both ask for at most one call per second
    db().execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, time.time(), json.dumps(data)))
    db().commit()
    return data


def weighted_sample(weights: dict, k: int, rng: random.Random) -> list:
    """k distinct keys, likelier the heavier they are. ponytail: O(k*n), fine for a few hundred artists."""
    pool = {key: max(w, 1e-9) for key, w in weights.items()}
    out = []
    while pool and len(out) < k:
        key = rng.choices(list(pool), weights=list(pool.values()))[0]
        out.append(key)
        del pool[key]
    return out


def lb_top_artists(rng_name: str, count: int = 100, offset: int = 0) -> list[dict]:
    data = cached(f"{LB_API}/stats/user/{urllib.parse.quote(LB_USER)}/artists", 1,
                  range=rng_name, count=count, offset=offset)
    return (data or {}).get("payload", {}).get("artists", [])


def similar_artists(mbid: str) -> list[tuple[str, str, float]]:
    """(mbid, name, similarity 0..1) for one artist, best first."""
    rows = [(a["artist_mbid"], a.get("name", ""), float(a.get("score") or 0))
            for a in cached(LB_SIMILAR, 30, artist_mbids=mbid, algorithm=LB_SIMILAR_ALGO) or []
            if a.get("artist_mbid") and a["artist_mbid"] != mbid]
    top = max((s for *_, s in rows), default=0) or 1
    return [(m, n, s / top) for m, n, s in rows[:40]]


def top_recordings(mbid: str) -> list[dict]:
    return cached(f"{LB_API}/popularity/top-recordings-for-artist/{mbid}", 30) or []


def top_album(mbid: str) -> dict | None:
    groups = cached(f"{LB_API}/popularity/top-release-groups-for-artist/{mbid}", 30) or []
    for g in groups:
        rg = g.get("release_group", {})
        if rg.get("type") == "Album" and rg.get("caa_release_mbid"):
            return g
    return None


def album_tracks(release_mbid: str) -> list[dict]:
    rel = cached(f"{MB_API}/release/{release_mbid}", 365, inc="recordings", fmt="json") or {}
    return [t for m in rel.get("media", []) for t in m.get("tracks", [])]


def artist_tags(mbid: str) -> set[str]:
    a = cached(f"{MB_API}/artist/{mbid}", 90, inc="tags+genres", fmt="json") or {}
    return {t["name"].lower() for t in a.get("tags", []) + a.get("genres", []) if t.get("name")}


def known_artists() -> tuple[set[str], set[str]]:
    """MBIDs and lower-cased names of everyone you have played or own."""
    mbids, names = set(), set()
    for page in range(5):
        artists = lb_top_artists("all_time", 1000, page * 1000)
        for a in artists:
            names.add(a.get("artist_name", "").lower())
            mbids.update(filter(None, [a.get("artist_mbid")] + (a.get("artist_mbids") or [])))
        if len(artists) < 1000:
            break
    nd = navidrome()
    if nd:
        try:
            for a in nd.artists():
                names.add(a.get("name", "").lower())
                if a.get("musicBrainzId"):
                    mbids.add(a["musicBrainzId"])
        except HttpError as e:
            log(f"discovery: could not read the Navidrome library ({e}); only ListenBrainz history is excluded")
    names.discard("")
    return mbids, names


def recently_picked(kind: str, days: float | None = None) -> set[str]:
    since = 0 if days is None else time.time() - days * 86400
    return {r[0] for r in db().execute("SELECT id FROM picks WHERE kind = ? AND at >= ?", (kind, since))}


def remember_picks(tracks: list[dict]) -> None:
    now = time.time()
    rows = [(k, v, now) for t in tracks for k, v in (("artist", t["_artist_mbid"]), ("recording", t["mbTrackId"]))
            if v]
    db().executemany("INSERT OR REPLACE INTO picks VALUES (?, ?, ?)", rows)
    db().commit()


def walk(seeds: dict[str, float], hops: int, fanout: int, rng: random.Random) -> dict[str, dict]:
    """Breadth-first through similar artists: {mbid: {'name', 'hop', 'score'}} for everything reached."""
    found: dict[str, dict] = {}
    visited = set(seeds)
    frontier = dict(seeds)
    for hop in range(1, hops + 1):
        nxt: dict[str, float] = {}
        names: dict[str, str] = {}
        for m in weighted_sample(frontier, fanout, rng):
            try:
                sims = similar_artists(m)
            except HttpError as e:
                log(f"discovery: similar artists for {m} failed: {e}")
                continue
            for sm, name, s in sims:
                if sm not in visited:
                    nxt[sm] = max(nxt.get(sm, 0.0), frontier[m] * s)
                    names[sm] = name
        if not nxt:
            break
        top = max(nxt.values())
        frontier = {m: w / top for m, w in nxt.items()}
        for m, w in frontier.items():
            found[m] = {"name": names[m], "hop": hop, "score": w}
        visited |= set(nxt)
    return found


def build_mix(mix: Mix, rng: random.Random | None = None) -> list[dict]:
    rng = rng or random.Random()
    top = lb_top_artists(mix.seed_range) or lb_top_artists("all_time")
    seeds = {a["artist_mbid"]: float(a.get("listen_count") or 1) for a in top if a.get("artist_mbid")}
    if not seeds:
        log("discovery: ListenBrainz has no artist statistics for you yet")
        return []
    seeds = {m: seeds[m] for m in weighted_sample(seeds, mix.seeds, rng)}
    log(f"discovery {mix.name}: walking {mix.hops[1]} hop(s) out from {len(seeds)} seed artists")

    known_mbids, known_names = known_artists()
    resting = recently_picked("artist", ARTIST_COOLDOWN_DAYS)
    lo, hi = mix.hops
    pool = {m: c for m, c in walk(seeds, hi, max(mix.seeds, 10), rng).items()
            if lo <= c["hop"] <= hi and m not in known_mbids and m not in resting
            and c["name"].lower() not in known_names}
    if AVOID_TAGS or PREFER_TAGS:  # tags cost one MusicBrainz call per artist, so only the likeliest 80
        checked = {}
        for m in sorted(pool, key=lambda m: pool[m]["score"], reverse=True)[:80]:
            try:
                tags = artist_tags(m)
            except HttpError:
                tags = set()
            if not tags & AVOID_TAGS:
                checked[m] = dict(pool[m], score=pool[m]["score"] * (3 if tags & PREFER_TAGS else 1))
        pool = checked
    log(f"discovery {mix.name}: {len(pool)} new artists in reach")

    played = recently_picked("recording")
    tracks: list[dict] = []
    keys: set[str] = set()
    albums = 0
    singles = 0
    for m in weighted_sample({m: c["score"] for m, c in pool.items()}, len(pool), rng):
        if singles >= mix.tracks and albums >= mix.albums:
            break
        try:
            if albums < mix.albums and (g := top_album(m)):
                artist = g.get("artist", {}).get("name") or pool[m]["name"]
                album = g["release_group"].get("name", "")
                got = [{"title": t.get("title", ""), "artist": artist, "mainArtist": artist, "release": album,
                        "mbTrackId": t.get("recording", {}).get("id", ""), "_artist_mbid": m}
                       for t in album_tracks(g["release_group"]["caa_release_mbid"])]
                if 3 <= len(got) <= 25:  # an album, not a single or a box set
                    log(f"discovery {mix.name}: album {artist} - {album}")
                    tracks += got
                    albums += 1
                    continue
            if singles >= mix.tracks:
                continue
            recs = [r for r in top_recordings(m) if r.get("recording_mbid") not in played]
            window = recs[mix.rank[0]:mix.rank[1]] or recs[:mix.rank[1]]
            rng.shuffle(window)
            for r in window:
                artist, title = r.get("artist_name", ""), r.get("recording_name", "")
                k = track_key(artist, title)
                if artist and title and k not in keys:
                    keys.add(k)
                    tracks.append({"title": title, "artist": artist, "mainArtist": artist,
                                   "release": r.get("release_name", ""), "mbTrackId": r["recording_mbid"],
                                   "_artist_mbid": m})
                    singles += 1
                    break
        except HttpError as e:
            log(f"discovery: skipping artist {m}: {e}")
    return tracks


# --------------------------------------------------------------------------
# Explo bridge
# --------------------------------------------------------------------------


def lb_playlist_name(d: dt.date | None = None) -> str:
    y, w = iso_week(d)
    return f"Weekly-Exploration-{y}-Week{w}"  # exactly how Explo names it with --replace-playlist=false


def register_custom_playlist(pid: str, name: str, tracks: list[dict]) -> None:
    """Write the two files `explo --playlist=custom-*` reads (Explo v1.2 cache format)."""
    cache_dir = os.path.join(CONFIG_DIR, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    with open(os.path.join(cache_dir, f"{pid}.json"), "w", encoding="utf-8") as f:
        json.dump({"tracks": tracks}, f, ensure_ascii=False)
    meta_path = os.path.join(CONFIG_DIR, "custom-playlists.json")
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, list):
            meta = []
    except (OSError, ValueError):
        meta = []
    meta = [m for m in meta if m.get("id") != pid]
    meta.append({"id": pid, "name": name, "source": "ainulindale", "refresh_days": 0,
                 "last_fetched": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)


def unregister_custom_playlist(pid: str) -> None:
    try:
        os.remove(os.path.join(CONFIG_DIR, "cache", f"{pid}.json"))
    except OSError:
        pass
    meta_path = os.path.join(CONFIG_DIR, "custom-playlists.json")
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = [m for m in json.load(f) if m.get("id") != pid]
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
    except (OSError, ValueError):
        pass


def run_explo(*flags: str) -> int:
    cmd = [EXPLO_BIN, "--config", EXPLO_ENV, *flags]
    log("running: " + " ".join(cmd))
    try:
        return subprocess.run(cmd, cwd=os.path.dirname(EXPLO_BIN) or ".", timeout=EXPLO_TIMEOUT_MIN * 60).returncode
    except subprocess.TimeoutExpired:
        log(f"Explo exceeded {EXPLO_TIMEOUT_MIN} min and was stopped")
        return 124
    except OSError as e:
        log(f"could not start Explo: {e}")
        return 127


# --------------------------------------------------------------------------
# Navidrome (Subsonic API)
# --------------------------------------------------------------------------


class Navidrome:
    def __init__(self, url: str, user: str, password: str):
        self.url, self.user, self.password = url, user, password

    def call(self, endpoint: str, **params) -> dict:
        salt = secrets.token_hex(8)
        params.update({"u": self.user, "s": salt, "t": hashlib.md5((self.password + salt).encode()).hexdigest(),
                       "v": "1.16.1", "c": "ainulindale", "f": "json"})
        resp = http_json(f"{self.url}/rest/{endpoint}", params=params, timeout=30).get("subsonic-response", {})
        if resp.get("status") != "ok":
            raise HttpError(f"Navidrome {endpoint}: {resp.get('error', {}).get('message', 'failed')}")
        return resp

    def playlists(self) -> list[dict]:
        return self.call("getPlaylists").get("playlists", {}).get("playlist", []) or []

    def playlist_entries(self, pid: str) -> list[dict]:
        return self.call("getPlaylist", id=pid).get("playlist", {}).get("entry", []) or []

    def artists(self) -> list[dict]:
        idx = self.call("getArtists").get("artists", {}).get("index", []) or []
        return [a for i in idx for a in i.get("artist", []) or []]

    def starred(self) -> list[dict]:
        return self.call("getStarred2").get("starred2", {}).get("song", []) or []

    def delete_playlist(self, pid: str) -> None:
        self.call("deletePlaylist", id=pid)

    def scan(self) -> None:
        self.call("startScan")


def navidrome() -> Navidrome | None:
    if NAVIDROME_URL and NAVIDROME_USER:
        return Navidrome(NAVIDROME_URL, NAVIDROME_USER, NAVIDROME_PASS)
    return None


# --------------------------------------------------------------------------
# Retention
# --------------------------------------------------------------------------

# (label, playlist-name regex, download-folder regex, how many to keep)
def families() -> list[tuple[str, re.Pattern, re.Pattern, int]]:
    fams = [
        ("ListenBrainz weekly exploration",
         re.compile(r"^Weekly-Exploration-(\d{4})-Week(\d{1,2})$"),
         re.compile(r"^Weekly-Exploration-(\d{4})-Week(\d{1,2})$"), KEEP_WEEKS),
        # pre-2.1 Last.fm playlists: no new ones are made, so they all go (starred tracks are rescued)
        ("old Last.fm recommendations",
         re.compile(r"^Lastfm-Recommended-(\d{4})-Week(\d{1,2})$"),
         re.compile(r"^custom-lastfm-(\d{4})-(\d{1,2})$", re.I), 0),
    ]
    for m in PRESETS.values():  # every preset, enabled or not, so a mix you switch off still ages out
        unit = {"day": "Day", "week": "Week", "month": "Month"}[m.period]
        fams.append((f"{m.name} discovery mix",
                     re.compile(rf"^Discovery-{m.name.title()}-(\d{{4}})-{unit}(\d{{1,3}})$"),
                     re.compile(rf"^custom-{m.name}-(\d{{4}})-(\d{{1,3}})$", re.I), KEEP_WEEKS))
    if PRUNE_LEGACY_JAMS:
        jams = re.compile(r"^(?:Weekly|Daily)-Jams(?:-(\d{4})-(?:Week|Day)(\d{1,3}))?$")
        fams.append(("legacy jams", jams, jams, 0))
    return fams


def split_keep(names: list[str], rx: re.Pattern, keep: int) -> tuple[list[str], list[str]]:
    def order(n: str):
        g = rx.match(n).groups()
        return int(g[0] or 0), int(g[1] or 0)
    # a set: the same name can exist twice in Navidrome (duplicates left by older setups)
    # and must only ever use up one of the "keep" slots
    ranked = sorted({n for n in names if rx.match(n)}, key=order, reverse=True)
    return ranked[:keep], ranked[keep:]


def rescue_or_delete(folder: str, protected: set[tuple[int, str]], dry: bool) -> tuple[int, int]:
    """Delete an expired download folder; files you starred or playlisted move to Keepers/ instead."""
    rescued = removed = 0
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            path = os.path.join(root, fn)
            ext = os.path.splitext(fn)[1].lower()
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if ext in AUDIO_EXT and (size, ext.lstrip(".")) in protected:
                rescued += 1
                if not dry:
                    os.makedirs(KEEPERS_DIR, exist_ok=True)
                    dest = os.path.join(KEEPERS_DIR, fn)
                    if os.path.exists(dest):
                        stem, e = os.path.splitext(fn)
                        dest = os.path.join(KEEPERS_DIR, f"{stem}.{int(time.time())}{e}")
                    shutil.move(path, dest)
            else:
                removed += 1
    if not dry:
        shutil.rmtree(folder, ignore_errors=True)
        if os.path.isdir(folder):
            log(f"  could not fully remove {folder} (permissions? files from an older root-run setup?)")
    return rescued, removed


def sweep_empty_slskd_dirs(dry: bool) -> None:
    """Explo moves finished files out of slskd's folder; drop empty shells older than a day."""
    if not os.path.isdir(SLSKD_DIR) or os.path.isdir(os.path.join(SLSKD_DIR, "explo")):
        return  # an explo/ folder in here means slskd downloads into the library root: hands off
    cutoff = time.time() - 86400
    for name in os.listdir(SLSKD_DIR):
        path = os.path.join(SLSKD_DIR, name)
        if name == "incomplete" or not os.path.isdir(path):
            continue
        try:
            if not os.listdir(path) and os.path.getmtime(path) < cutoff:
                log(f"  removing empty slskd folder {name}")
                if not dry:
                    os.rmdir(path)
        except OSError:
            pass


def prune(dry: bool = False) -> None:
    tag = "[dry-run] " if dry else ""
    nd = navidrome()
    doomed_playlists: list[dict] = []
    all_playlists: list[dict] = []
    if nd:
        try:
            all_playlists = nd.playlists()
        except HttpError as e:
            log(f"prune: Navidrome unreachable ({e}); nothing pruned this time")
            return
    try:
        folders = [d for d in os.listdir(DATA_DIR) if os.path.isdir(os.path.join(DATA_DIR, d))]
    except OSError:
        folders = []

    doomed_folders: list[str] = []
    for label, pl_rx, dir_rx, keep in families():
        kept, old = split_keep([p["name"] for p in all_playlists], pl_rx, keep)
        doomed_playlists += [p for p in all_playlists if p["name"] in old]
        for name in kept:  # duplicates of a week that stays: keep the fullest copy only
            twins = sorted((p for p in all_playlists if p["name"] == name),
                           key=lambda p: int(p.get("songCount") or 0), reverse=True)
            if len(twins) > 1:
                log(f"{tag}prune {label}: {len(twins) - 1} duplicate(s) of {name}")
                doomed_playlists += twins[1:]
        _, old_dirs = split_keep(folders, dir_rx, keep)
        doomed_folders += old_dirs
        if old or old_dirs:
            log(f"{tag}prune {label}: {len(old)} playlist(s), {len(old_dirs)} folder(s) beyond the newest {keep}")

    if not doomed_playlists and not doomed_folders:
        sweep_empty_slskd_dirs(dry)
        return

    # Anything starred, or sitting in a playlist that survives, must not be deleted.
    # Navidrome hides real paths by default, so files are matched by exact byte size + extension.
    protected: set[tuple[int, str]] = set()
    if nd and PRUNE_FILES and KEEP_FAVOURITES and doomed_folders:
        try:
            doomed_ids = {p["id"] for p in doomed_playlists}
            songs = nd.starred()
            for p in all_playlists:
                if p["id"] not in doomed_ids:
                    songs += nd.playlist_entries(p["id"])
            protected = {(int(s["size"]), str(s.get("suffix", "")).lower()) for s in songs if s.get("size")}
        except HttpError as e:
            log(f"prune: could not read favourites ({e}); keeping files this time to be safe")
            doomed_folders = []

    for p in doomed_playlists:
        log(f"{tag}  delete playlist {p['name']}")
        if not dry:
            try:
                nd.delete_playlist(p["id"])
            except HttpError as e:
                log(f"  failed: {e}")
            m = re.match(r"^(?:Discovery-(\w+)|Lastfm-Recommended)-(\d{4})-[A-Za-z]+(\d{1,3})$", p["name"])
            if m:  # forget the matching entry in Explo's custom playlist registry
                unregister_custom_playlist(f"custom-{(m.group(1) or 'lastfm').lower()}-{m.group(2)}-{int(m.group(3))}")

    if PRUNE_FILES:
        for d in doomed_folders:
            rescued, removed = rescue_or_delete(os.path.join(DATA_DIR, d), protected, dry)
            log(f"{tag}  delete folder {d}: {removed} file(s) removed, {rescued} kept in Keepers/")
        if doomed_folders and nd and not dry:
            try:
                nd.scan()
            except HttpError:
                pass  # needs an admin user; Navidrome's own scheduled scan will catch up

    sweep_empty_slskd_dirs(dry)


# --------------------------------------------------------------------------
# The tick
# --------------------------------------------------------------------------


def delete_playlist_named(name: str) -> None:
    nd = navidrome()
    if not nd:
        return
    try:
        for p in nd.playlists():
            if p["name"] == name:
                log(f"removing earlier partial playlist {name}")
                nd.delete_playlist(p["id"])
    except HttpError as e:
        log(f"could not check for an existing '{name}' playlist: {e}")


def step_listenbrainz(state: dict) -> str:
    """Returns: imported | current | waiting | down | failed | disabled"""
    if not (LB_ENABLED and LB_USER):
        return "disabled"
    lb = state["listenbrainz"]
    try:
        latest = lb_latest_playlist(LB_USER)
    except HttpError as e:
        log(f"ListenBrainz is not answering: {e}")
        return "down"
    if not latest or not latest["mbid"]:
        log("ListenBrainz has no Weekly Exploration playlist for this user yet")
        return "waiting"
    if latest["mbid"] == lb.get("imported_mbid"):
        log(f"ListenBrainz: newest playlist ({latest['date'][:10]}) is already imported")
        return "current" if lb_is_fresh(state) else "waiting"
    if lb.get("attempt_mbid") == latest["mbid"] and lb.get("attempts", 0) >= LB_MAX_ATTEMPTS:
        log(f"ListenBrainz: giving up on playlist {latest['mbid']} after {LB_MAX_ATTEMPTS} failed attempts")
        return "failed"

    log(f"ListenBrainz: new playlist '{latest['title']}' ({latest['date'][:10]})")
    if not slskd_ready():
        return "failed"
    lb["attempts"] = lb.get("attempts", 0) + 1 if lb.get("attempt_mbid") == latest["mbid"] else 1
    lb["attempt_mbid"] = latest["mbid"]
    save_state(state)

    delete_playlist_named(lb_playlist_name())
    rc = run_explo("--playlist=weekly-exploration", "--replace-playlist=false")
    if rc != 0:
        log(f"Explo exited with code {rc} on the ListenBrainz import (attempt {lb['attempts']}/{LB_MAX_ATTEMPTS})")
        return "failed"
    lb.update({"imported_mbid": latest["mbid"], "imported_date": latest["date"],
               "imported_at": dt.datetime.now().isoformat(timespec="seconds"), "attempts": 0})
    save_state(state)
    return "imported"


def step_mix(state: dict, mix: Mix) -> str:
    """Returns: imported | current | failed | disabled"""
    if not (LB_USER and LB_TOKEN):
        log(f"discovery {mix.name}: needs LISTENBRAINZ_USER and LISTENBRAINZ_USER_TOKEN")
        return "disabled"
    st = state["mixes"].setdefault(mix.name, {})
    pid, name = mix.ids()
    if st.get("playlist") == name:
        return "current"
    try:
        tracks = build_mix(mix)
    except HttpError as e:
        log(f"discovery {mix.name}: ListenBrainz is not answering: {e}")
        return "failed"
    if len(tracks) < min(5, mix.tracks):
        log(f"discovery {mix.name}: only {len(tracks)} usable tracks; will retry on the next tick")
        return "failed"
    if not slskd_ready():
        return "failed"
    register_custom_playlist(pid, name, tracks)
    rc = run_explo(f"--playlist={pid}")  # replace-playlist defaults to true: a re-run overwrites, never duplicates
    if rc != 0:
        log(f"Explo exited with code {rc} on {name}")
        return "failed"
    remember_picks(tracks)
    st.update({"playlist": name, "tracks": len(tracks), "imported_at": dt.datetime.now().isoformat(timespec="seconds")})
    save_state(state)
    return "imported"


def tick() -> int:
    state = load_state()
    lb_status = step_listenbrainz(state)
    mixes = {m.name: step_mix(state, m) for m in configured_mixes()}
    try:
        prune()
    except Exception as e:  # pruning must never take the run down with it
        log(f"prune failed: {type(e).__name__}: {e}")
    state = load_state()
    state["last_tick"] = {"at": dt.datetime.now().isoformat(timespec="seconds"),
                          "listenbrainz": lb_status, "mixes": mixes}
    save_state(state)
    log("done: " + " ".join(f"{k}={v}" for k, v in {"listenbrainz": lb_status, **mixes}.items()))
    return 75 if "failed" in (lb_status, *mixes.values()) else 0


def status() -> int:
    print(json.dumps(load_state(), indent=2))
    n, = db().execute("SELECT COUNT(*) FROM cache").fetchone()
    picks = dict(db().execute("SELECT kind, COUNT(*) FROM picks GROUP BY kind").fetchall())
    print(f"cache: {n} lookups, history: {picks.get('artist', 0)} artists / {picks.get('recording', 0)} tracks")
    if USES_SLSKD and SLSKD_URL and SLSKD_API_KEY:
        try:
            print("slskd:", Slskd(SLSKD_URL, SLSKD_API_KEY).server_state().get("state"))
        except HttpError as e:
            print("slskd: unreachable -", e)
    if LB_USER:
        try:
            print("listenbrainz newest:", lb_latest_playlist(LB_USER))
        except HttpError as e:
            print("listenbrainz: down -", e)
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "status":
        return status()
    if cmd == "preview":  # what a mix would pick right now; nothing is downloaded or remembered
        mix = PRESETS.get(sys.argv[2] if len(sys.argv) > 2 else "weekly")
        if not (mix and LB_USER and LB_TOKEN):
            print("usage: preview [daily|weekly|monthly], with LISTENBRAINZ_USER and LISTENBRAINZ_USER_TOKEN set")
            return 2
        for t in build_mix(mix):
            print(f"{t['artist']} - {t['title']}" + (f"  [{t['release']}]" if t["release"] else ""))
        return 0
    if cmd not in ("run", "prune"):
        print(__doc__)
        return 2

    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("another run is still in progress, skipping")
            return 0
        if cmd == "prune":
            prune(dry="--dry-run" in sys.argv)
            return 0
        return tick()


if __name__ == "__main__":
    sys.exit(main())
