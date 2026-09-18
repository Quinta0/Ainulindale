#!/usr/bin/env python3
"""
Ainulindale discovery orchestrator.

Runs inside the Explo container (python 3.12, stdlib only) and drives the Explo
binary instead of Explo's built-in cron. One idempotent "tick" does this:

  1. Preflight   make sure slskd is actually logged in to Soulseek, otherwise
                 Explo would silently fall back to YouTube for the whole week.
  2. ListenBrainz  import Weekly Exploration, but only if ListenBrainz has
                 published a playlist we have not imported yet.
  3. Last.fm     build a playlist from Last.fm recommendations and hand it to
                 Explo as a "custom" playlist (always, or only as a fallback).
  4. Prune       keep the newest N weeks per source, remove older playlists
                 and their files; starred / playlisted tracks are rescued.

Because every step checks state first, the tick can run as often as you like
and after any restart without ever producing duplicates.

Usage:  discover.py run | status | prune [--dry-run] | lastfm-preview
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

LASTFM_USER = env("LASTFM_USER")
LASTFM_API_KEY = env("LASTFM_API_KEY")
LASTFM_MODE = env("LASTFM_MODE", "always").lower()  # always | fallback | off
LASTFM_STATIONS = [s for s in env("LASTFM_STATIONS", "recommended").split(",") if s.strip()]
LASTFM_TRACKS = env_int("LASTFM_TRACKS", 30)
LASTFM_FALLBACK_DAY = env_int("LASTFM_FALLBACK_DAY", 3)  # ISO weekday, 3 = Wednesday
LASTFM_HISTORY = 1000  # remembered recommendations, so weeks do not repeat each other

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

USER_AGENT = "Mozilla/5.0 (compatible; Ainulindale/2.0; +https://github.com/Quinta0/Ainulindale)"
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
    state.setdefault("lastfm", {})
    state["lastfm"].setdefault("history", [])
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
# Last.fm
# --------------------------------------------------------------------------


def track_key(artist: str, title: str) -> str:
    return re.sub(r"[^\w]+", "", f"{artist}|{title}".lower(), flags=re.UNICODE)


def lastfm_station(user: str, station: str, want: int, seen: set[str]) -> list[dict]:
    """
    Last.fm's own recommendation engine, via the JSON the website player uses.
    Undocumented but public and stable for years; every call returns a fresh
    batch, so we poll until we have enough unseen tracks.
    """
    url = f"https://www.last.fm/player/station/user/{urllib.parse.quote(user)}/{station}"
    out: list[dict] = []
    dry = 0
    for _ in range(25):
        if len(out) >= want or dry >= 3:
            break
        batch = http_json(url, timeout=30).get("playlist", [])
        if not batch:
            break
        added = 0
        for t in batch:
            title = (t.get("name") or t.get("_name") or "").strip()
            artists = [a.get("name") or a.get("_name") or "" for a in t.get("artists", [])]
            artists = [a.strip() for a in artists if a.strip()]
            if not title or not artists:
                continue
            key = track_key(artists[0], title)
            if key in seen:
                continue
            seen.add(key)
            out.append({"title": title, "artist": ", ".join(artists), "mainArtist": artists[0], "release": ""})
            added += 1
        dry = 0 if added else dry + 1
        time.sleep(1)
    return out[:want]


def lastfm_api(method: str, **params) -> dict:
    params.update({"method": method, "api_key": LASTFM_API_KEY, "format": "json"})
    data = http_json("https://ws.audioscrobbler.com/2.0/", params=params, timeout=20)
    if "error" in data:
        raise HttpError(f"Last.fm API error {data['error']}: {data.get('message')}")
    time.sleep(0.25)  # stay well under the 5 req/s limit
    return data


def lastfm_similar(user: str, want: int, seen: set[str]) -> list[dict]:
    """
    Fallback built only on the official API: artists similar to what you played
    in the last 3 months, minus every artist you already know, one top track each.
    """
    known = set()
    for page in (1, 2):
        top = lastfm_api("user.gettopartists", user=user, period="overall", limit=1000, page=page)
        artists = top.get("topartists", {}).get("artist", [])
        known.update(a["name"].lower() for a in artists)
        if len(artists) < 1000:
            break
    recent = lastfm_api("user.gettopartists", user=user, period="3month", limit=30)
    scores: dict[str, float] = {}
    for seed in recent.get("topartists", {}).get("artist", []):
        try:
            sim = lastfm_api("artist.getsimilar", artist=seed["name"], limit=15, autocorrect=1)
        except HttpError:
            continue
        for a in sim.get("similarartists", {}).get("artist", []):
            if a["name"].lower() not in known:
                scores[a["name"]] = scores.get(a["name"], 0.0) + float(a.get("match") or 0)
    ranked = sorted(scores, key=scores.get, reverse=True)
    out: list[dict] = []
    for artist in ranked:
        if len(out) >= want:
            break
        try:
            top = lastfm_api("artist.gettoptracks", artist=artist, limit=5, autocorrect=1)
        except HttpError:
            continue
        candidates = [t["name"] for t in top.get("toptracks", {}).get("track", []) if t.get("name")]
        random.shuffle(candidates)
        for title in candidates:
            key = track_key(artist, title)
            if key not in seen:
                seen.add(key)
                out.append({"title": title, "artist": artist, "mainArtist": artist, "release": ""})
                break
    return out


def lastfm_fill_albums(tracks: list[dict]) -> None:
    """Optional nicety when an API key is present: album names make Explo's matching stricter."""
    for t in tracks:
        try:
            info = lastfm_api("track.getinfo", artist=t["mainArtist"], track=t["title"], autocorrect=1)
            t["release"] = info.get("track", {}).get("album", {}).get("title", "") or ""
        except HttpError:
            pass


def lastfm_recommendations(state: dict, want: int) -> list[dict]:
    seen = set(state["lastfm"]["history"])
    tracks: list[dict] = []
    for station in LASTFM_STATIONS:
        if len(tracks) >= want:
            break
        try:
            got = lastfm_station(LASTFM_USER, station.strip(), want - len(tracks), seen)
            log(f"Last.fm station '{station}': {len(got)} new tracks")
            tracks += got
        except HttpError as e:
            log(f"Last.fm station '{station}' failed: {e}")
    if len(tracks) < want and LASTFM_API_KEY:
        try:
            got = lastfm_similar(LASTFM_USER, want - len(tracks), seen)
            log(f"Last.fm similar-artists (API): {len(got)} new tracks")
            tracks += got
        except HttpError as e:
            log(f"Last.fm API fallback failed: {e}")
    if tracks and LASTFM_API_KEY:
        lastfm_fill_albums(tracks)
    return tracks


# --------------------------------------------------------------------------
# Explo bridge
# --------------------------------------------------------------------------


def lb_playlist_name(d: dt.date | None = None) -> str:
    y, w = iso_week(d)
    return f"Weekly-Exploration-{y}-Week{w}"  # exactly how Explo names it with --replace-playlist=false


def lastfm_ids(d: dt.date | None = None) -> tuple[str, str]:
    y, w = iso_week(d)
    # digits only after the prefix: Explo title-cases the id to build the download folder name
    return f"custom-lastfm-{y}-{w}", f"Lastfm-Recommended-{y}-Week{w}"


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
    meta.append({"id": pid, "name": name, "source": "lastfm", "refresh_days": 0,
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
        ("Last.fm recommendations",
         re.compile(r"^Lastfm-Recommended-(\d{4})-Week(\d{1,2})$"),
         re.compile(r"^custom-lastfm-(\d{4})-(\d{1,2})$", re.I), KEEP_WEEKS),
    ]
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
            m = re.match(r"^Lastfm-Recommended-(\d{4})-Week(\d{1,2})$", p["name"])
            if m:  # forget the matching entry in Explo's custom playlist registry
                unregister_custom_playlist(f"custom-lastfm-{m.group(1)}-{int(m.group(2))}")

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


def step_lastfm(state: dict, lb_status: str) -> str:
    if LASTFM_MODE == "off" or not LASTFM_USER:
        return "disabled"
    lf = state["lastfm"]
    if lf.get("week") == week_key():
        log("Last.fm: this week's playlist already exists")
        return "current"
    if LASTFM_MODE == "fallback":
        if lb_status in ("imported", "current"):
            return "not needed"
        if dt.date.today().isoweekday() < LASTFM_FALLBACK_DAY:
            log("Last.fm fallback: giving ListenBrainz a little longer before stepping in")
            return "waiting"
        log(f"Last.fm fallback: ListenBrainz is '{lb_status}', stepping in")

    tracks = lastfm_recommendations(state, LASTFM_TRACKS)
    if len(tracks) < min(5, LASTFM_TRACKS):
        log(f"Last.fm gave only {len(tracks)} usable tracks; will retry on the next tick")
        return "failed"
    if not slskd_ready():
        return "failed"

    pid, name = lastfm_ids()
    register_custom_playlist(pid, name, tracks)
    rc = run_explo(f"--playlist={pid}")  # replace-playlist defaults to true: a re-run overwrites, never duplicates
    if rc != 0:
        log(f"Explo exited with code {rc} on the Last.fm import")
        return "failed"
    lf["week"] = week_key()
    lf["history"] = (lf["history"] + [track_key(t["mainArtist"], t["title"]) for t in tracks])[-LASTFM_HISTORY:]
    save_state(state)
    return "imported"


def tick() -> int:
    state = load_state()
    lb_status = step_listenbrainz(state)
    lf_status = step_lastfm(state, lb_status)
    try:
        prune()
    except Exception as e:  # pruning must never take the run down with it
        log(f"prune failed: {type(e).__name__}: {e}")
    state = load_state()
    state["last_tick"] = {"at": dt.datetime.now().isoformat(timespec="seconds"),
                          "listenbrainz": lb_status, "lastfm": lf_status}
    save_state(state)
    log(f"done: listenbrainz={lb_status} lastfm={lf_status}")
    return 75 if "failed" in (lb_status, lf_status) else 0


def status() -> int:
    state = load_state()
    print(json.dumps({k: v for k, v in state.items() if k != "lastfm"}, indent=2))
    lf = dict(state["lastfm"])
    lf["history"] = f"{len(lf.get('history', []))} remembered tracks"
    print(json.dumps({"lastfm": lf}, indent=2))
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
    if cmd == "lastfm-preview":
        if not LASTFM_USER:
            print("LASTFM_USER is not set")
            return 2
        for t in lastfm_recommendations(load_state(), LASTFM_TRACKS):
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
