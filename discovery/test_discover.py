"""Offline self-check for the discovery mixes: python3 discovery/test_discover.py"""
import datetime as dt
import os
import random
import re
import sys
import tempfile

os.environ.update(WEB_DATA_PATH=tempfile.mkdtemp(), LISTENBRAINZ_USER="me", LISTENBRAINZ_USER_TOKEN="t")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discover as d  # noqa: E402

# seeds A, B; K is already known. hop 1: C D E, hop 2: F G H
GRAPH = {"A": ["C", "D", "K"], "B": ["D", "E"], "C": ["F"], "D": ["G"], "E": ["H"], "K": ["A"]}
ALBUM_OF = {"F"}


def fake_cached(url, ttl, **p):
    if "/stats/user/" in url:
        names = ["A", "B"] if p["range"] != "all_time" else ["A", "B", "K"]
        return {"payload": {"artists": [{"artist_mbid": n, "artist_name": f"n{n}", "listen_count": 10}
                                        for n in names] if p.get("offset", 0) == 0 else []}}
    if url == d.LB_SIMILAR:
        return [{"artist_mbid": m, "name": f"n{m}", "score": 100 - i} for i, m in enumerate(GRAPH.get(p["artist_mbids"], []))]
    m = url.rstrip("/").rsplit("/", 1)[-1]
    if "top-recordings" in url:
        return [{"artist_name": f"n{m}", "recording_name": f"{m}-song{i}", "recording_mbid": f"{m}-r{i}",
                 "release_name": ""} for i in range(30)]
    if "top-release-groups" in url:
        return [{"artist": {"name": f"n{m}"}, "release_group": {"type": "Album", "name": f"{m}-lp",
                                                                "caa_release_mbid": f"rel-{m}"}}] if m in ALBUM_OF else []
    if "/release/" in url:
        return {"media": [{"tracks": [{"title": f"{m}-t{i}", "recording": {"id": f"{m}-t{i}"}} for i in range(5)]}]}
    raise AssertionError(url)


d.cached = fake_cached
d.navidrome = lambda: None
d.log = lambda *_: None


def artists(tracks):
    return {t["_artist_mbid"] for t in tracks}


daily = d.build_mix(d.PRESETS["daily"], random.Random(1))
assert artists(daily) == {"C", "D", "E"}, artists(daily)  # close: hop 1 only, never known K or seeds
assert len({d.track_key(t["artist"], t["title"]) for t in daily}) == len(daily)
assert all(int(t["title"].split("song")[1]) < 3 for t in daily)  # daily takes the hits

monthly = d.build_mix(d.PRESETS["monthly"], random.Random(1))
assert artists(monthly) == {"F", "G", "H"}, artists(monthly)  # deep: hops 2-3
assert sum(t["release"] == "F-lp" for t in monthly) == 5  # F came as a whole album
assert all(int(t["title"].split("song")[1]) >= 3 for t in monthly if "song" in t["title"])  # and skips the hits

d.remember_picks(daily)
assert artists(d.build_mix(d.PRESETS["daily"], random.Random(2))) == set()  # recommended artists rest

for mix in d.PRESETS.values():  # names Explo produces are the names retention recognises
    pid, name = mix.ids(dt.date(2026, 11, 27))
    fam = next(f for f in d.families() if f[0].startswith(mix.name))
    assert fam[1].match(name) and fam[2].match(pid.title()), (name, pid)
    m = re.match(r"^(?:Discovery-(\w+)|Lastfm-Recommended)-(\d{4})-[A-Za-z]+(\d{1,3})$", name)
    assert f"custom-{m.group(1).lower()}-{m.group(2)}-{int(m.group(3))}" == pid
assert d.PRESETS["daily"].ids(dt.date(2026, 11, 27))[1] == "Discovery-Daily-2026-Day331"
print("ok")
