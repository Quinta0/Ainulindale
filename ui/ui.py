#!/usr/bin/env python3
"""
Ainulindale web UI: what the container is doing, and a Soulseek search.

Stdlib only. Reuses discover.py's clients and state. Behind HTTP basic auth
with the slskd web login (SLSKD_USERNAME / SLSKD_PASSWORD).
"""

from __future__ import annotations

import base64
import collections
import fcntl
import hmac
import json
import os
import subprocess
import sys
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/ainulindale")
import discover as d  # noqa: E402

PORT = int(os.environ.get("UI_PORT", "7288"))
USER = os.environ.get("SLSKD_USERNAME", "")
PASS = os.environ.get("SLSKD_PASSWORD", "")
GLUETUN = "http://localhost:8000"
GLUETUN_KEY = os.environ.get("GLUETUN_API_KEY", "")
LOG_FILE = "/config/logs/discovery.log"
JOB_FILE = "/run/ainulindale.job"
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
AUDIO = {e.lstrip(".") for e in d.AUDIO_EXT}

slskd = d.Slskd(d.SLSKD_URL, d.SLSKD_API_KEY)


def sk(path: str, method: str = "GET", body=None, timeout: int = 15):
    """slskd API call with a JSON body (http_json only sends empty bodies)."""
    data = None if body is None else json.dumps(body).encode()
    req = d.urllib.request.Request(f"{d.SLSKD_URL}/api/v0/{path}", method=method, data=data, headers={
        "X-API-Key": d.SLSKD_API_KEY, "Content-Type": "application/json", "Accept": "application/json"})
    with d.urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else None


def tick_running() -> bool:
    try:
        with open(d.LOCK_FILE, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def status() -> dict:
    out: dict = {"state": d.load_state(), "schedule": os.environ.get("DISCOVERY_SCHEDULE", "15 0 * * *"),
                 "running": tick_running(), "keep_weeks": d.KEEP_WEEKS}
    out["state"]["lastfm"] = {k: v for k, v in out["state"]["lastfm"].items() if k != "history"}
    try:
        out["slskd"] = slskd.server_state().get("state")
    except d.HttpError as e:
        out["slskd"] = f"unreachable ({e})"
    try:
        h = {"X-API-Key": GLUETUN_KEY}
        ip = d.http_json(f"{GLUETUN}/v1/publicip/ip", headers=h, timeout=5, retries=0)
        port = d.http_json(f"{GLUETUN}/v1/portforward", headers=h, timeout=5, retries=0)
        out["vpn"] = {"ip": ip.get("public_ip"), "country": ip.get("country"), "port": port.get("port")}
    except (d.HttpError, AttributeError):
        out["vpn"] = None
    nd = d.navidrome()
    try:
        fams = [rx for _, rx, _, _ in d.families()]
        out["playlists"] = sorted(
            ({"name": p["name"], "songs": p.get("songCount", 0)} for p in (nd.playlists() if nd else [])
             if any(rx.match(p["name"]) for rx in fams)),
            key=lambda p: p["name"], reverse=True)
    except d.HttpError as e:
        out["playlists"] = None
        out["navidrome_error"] = str(e)
    return out


def log_tail(lines: int = 200) -> str:
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            return "".join(collections.deque(f, lines))
    except OSError:
        return ""


def search_results(sid: str) -> dict:
    meta = sk(f"searches/{sid}")
    files = []
    for r in sk(f"searches/{sid}/responses") or []:
        for f in r.get("files", []):
            ext = (f.get("extension") or f["filename"].rsplit(".", 1)[-1]).lower().lstrip(".")
            if ext not in AUDIO:
                continue
            path = f["filename"].replace("\\", "/")
            folder, _, name = path.rpartition("/")
            files.append({"user": r["username"], "filename": f["filename"], "size": f.get("size", 0),
                          "name": name, "folder": folder.rsplit("/", 1)[-1], "dir": folder, "ext": ext,
                          "bitRate": f.get("bitRate"), "bitDepth": f.get("bitDepth"),
                          "sampleRate": f.get("sampleRate"), "length": f.get("length"),
                          "free": r.get("hasFreeUploadSlot", False), "speed": r.get("uploadSpeed", 0),
                          "queue": r.get("queueLength", 0)})
    lossless = {"flac", "wav", "aiff", "ape"}
    files.sort(key=lambda f: (f["ext"] not in lossless, not f["free"], -(f["bitRate"] or 0), -f["speed"]))
    return {"done": meta.get("isComplete", False), "state": meta.get("state"), "files": files[:300]}


def downloads() -> list:
    out = []
    for u in sk("transfers/downloads") or []:
        for dr in u.get("directories", []):
            for f in dr.get("files", []):
                out.append({"user": u["username"], "name": f["filename"].replace("\\", "/").rsplit("/", 1)[-1],
                            "state": f.get("state", ""), "percent": round(f.get("percentComplete") or 0),
                            "at": f.get("requestedAt", "")})
    out.sort(key=lambda f: f["at"], reverse=True)
    return out[:60]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # keep the container log for the pipeline
        pass

    def authed(self) -> bool:
        if not (USER and PASS):
            return True
        got = self.headers.get("Authorization", "")
        want = "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode()
        if hmac.compare_digest(got.encode(), want.encode()):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ainulindale"')
        self.end_headers()
        return False

    def send(self, code: int, body, ctype: str = "application/json") -> None:
        raw = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        if not self.authed():
            return
        url = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/":
                with open(PAGE, "rb") as f:
                    return self.send(200, f.read(), "text/html")
            if url.path == "/api/status":
                return self.send(200, status())
            if url.path == "/api/log":
                return self.send(200, log_tail(), "text/plain")
            if url.path == "/api/search":
                return self.send(200, search_results(str(uuid.UUID(q["id"][0]))))
            if url.path == "/api/downloads":
                return self.send(200, downloads())
            self.send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001 - one bad backend call must not kill the page
            self.send(502, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        if not self.authed():
            return
        # same-origin only: a page elsewhere on the LAN must not drive downloads with the browser's login
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
            return self.send(403, {"error": "cross-origin request refused"})
        try:
            if self.path == "/api/run":
                if tick_running():
                    return self.send(409, {"error": "a tick is already running"})
                subprocess.Popen(["sh", JOB_FILE], start_new_session=True)
                return self.send(202, {"ok": True})
            if self.path == "/api/prune-preview":
                r = subprocess.run(["sh", "/run/ainulindale-py", "prune", "--dry-run"],
                                   capture_output=True, text=True, timeout=120)
                return self.send(200, {"output": (r.stdout + r.stderr).strip() or "nothing to prune"})
            if self.path == "/api/search":
                text = str(self.body().get("q", "")).strip()[:200]
                if len(text) < 3:
                    return self.send(400, {"error": "type at least 3 characters"})
                sid = str(uuid.uuid4())
                sk("searches", "POST", {"id": sid, "searchText": text})
                return self.send(201, {"id": sid})
            if self.path == "/api/download":
                b = self.body()
                files = [{"filename": f["filename"], "size": int(f["size"])} for f in b.get("files", [])][:200]
                if not (b.get("user") and files):
                    return self.send(400, {"error": "nothing to download"})
                sk(f"transfers/downloads/{urllib.parse.quote(b['user'], safe='')}", "POST", files, timeout=60)
                return self.send(201, {"queued": len(files)})
            self.send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            self.send(502, {"error": f"{type(e).__name__}: {e}"})


if __name__ == "__main__":
    if not (USER and PASS):
        d.log("WARN: web UI has no login; set the slskd web username and password")
    d.log(f"web UI on :{PORT}")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
