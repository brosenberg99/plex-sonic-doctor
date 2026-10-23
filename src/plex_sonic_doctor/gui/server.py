"""Local web server for the GUI.

Only this computer can reach it: it listens on 127.0.0.1, checks the Host header, and every API
call must carry a secret made fresh at each launch. A web page from elsewhere can't read
the secret, so it can't make this app move files.
"""
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import __version__, acoustid, config, fingerprint, mover, plex, plexdata, plexlists, scan, setup_wizard
from ..config import PathMap
from ..pathmap import PathMapper, share_found, suggest_map
from .state import COMMON_FILTERS, State

STATIC = os.path.join(os.path.dirname(__file__), "static")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".svg": "image/svg+xml"}
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class Jobs:
    """One background job at a time (scan, damage check, move, undo...), with progress."""

    def __init__(self):
        self.lock = threading.Lock()
        self.info = {"kind": "", "running": False, "message": "", "fraction": -1, "log": [], "error": "",
                     "result": None, "started": 0, "finished": 0}
        self.stop_flag = False

    def start(self, kind, fn):
        with self.lock:
            if self.info["running"]:
                raise RuntimeError(f"Busy: {self.info['kind']} is still running")
            self.stop_flag = False
            self.info = {"kind": kind, "running": True, "message": "Starting", "fraction": -1, "log": [],
                         "error": "", "result": None, "started": time.time(), "finished": 0}

        def progress(msg, frac=-1):
            with self.lock:
                self.info["message"], self.info["fraction"] = msg, frac
                if not self.info["log"] or self.info["log"][-1][1].split(":")[0] != msg.split(":")[0]:
                    self.info["log"].append((time.strftime("%H:%M:%S"), msg))
                    self.info["log"] = self.info["log"][-200:]
                else:
                    self.info["log"][-1] = (time.strftime("%H:%M:%S"), msg)

        def run():
            try:
                result = fn(progress, lambda: self.stop_flag)
                with self.lock:
                    self.info["result"] = result
            except scan.Stopped:
                with self.lock:
                    self.info["error"] = "Stopped."
            except Exception as e:
                traceback.print_exc()
                with self.lock:
                    self.info["error"] = friendly_error(e, config.load())
            finally:
                with self.lock:
                    self.info["running"], self.info["finished"] = False, time.time()

        threading.Thread(target=run, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.info, default=str))


class App:
    def __init__(self):
        self.state = State()
        self.jobs = Jobs()
        self.secret = secrets.token_urlsafe(24)
        self.setup = {"pin": None, "token": None, "servers": [], "status": ""}

    # ---------------------------------------------------------------- API (name -> handler)

    def api(self, method, path, q, body):
        s, cfg = self.state, self.state.cfg
        route = (method, path)
        page = lambda: int(q.get("page", 1))
        # every list also takes the shared filters (artist, album, folder, source, years) and a sort
        filt = lambda *names: {n: q.get(n, "").lower() if n == "q" else q.get(n, "")
                               for n in (*names, *COMMON_FILTERS, "sort", "desc")}

        if route == ("GET", "/api/state"):
            backend, why = fingerprint.detect(cfg.ffmpeg, cfg.fpcalc)
            r = s.results
            return {"version": __version__, "configured": cfg.configured, "server": cfg.server_name or cfg.plex_url,
                    "sonic_dir": cfg.sonic_dir, "path_maps": [m.__dict__ for m in cfg.path_maps],
                    "tools": why if backend else f"{why}. {fingerprint.install_help()}", "tools_ok": bool(backend),
                    "results": {"created": r["created"], "stats": s.live_stats()} if r else None,
                    "tags": s.tag_summary(), "acoustid": bool(cfg.acoustid_key),
                    "marked": s.marked_summary(), "moved": len(s.moved), "job": self.jobs.snapshot(),
                    "data_dir": config.data_dir(), "reports_dir": config.sub_dir("reports")}
        if route == ("GET", "/api/job"):
            return self.jobs.snapshot()
        if route == ("GET", "/api/identical"):
            return s.identical_page(page(), **filt("q", "status", "note", "marked", "fmt"))
        if route == ("GET", "/api/versions"):
            return s.versions_page(page(), **filt("q", "reason", "marked"))
        if route == ("GET", "/api/broken"):
            return s.broken_page(page(), **filt("q", "severity", "problem"))
        if route == ("GET", "/api/broken-albums"):
            return s.albums_page(page(), **filt("q", "severity", "problem", "status"))
        if route == ("GET", "/api/facets"):
            return s.facets(q.get("tab", ""))
        if route == ("POST", "/api/recheck"):
            if "folders" in body:
                folders = list(body["folders"])
            else:
                f = body.get("filters", {})
                folders = s.album_folders(**{k: (f.get(k, "").lower() if k == "q" else f.get(k, ""))
                                             for k in ("q", "severity", "problem", "status", *COMMON_FILTERS)})
            def job(progress, should_stop):
                return s.recheck_albums(folders, progress, should_stop)
            self.jobs.start("Recheck", job)
            return {"ok": True, "albums": len(folders)}
        if route == ("POST", "/api/album-skip"):
            s.skip_album(body["folder"], body.get("skipped", True))
            return {"ok": True}
        if route == ("POST", "/api/mark-old-copies"):
            return {"changed": s.mark_old_copies(body["folder"]), "marked": s.marked_summary()}
        if route == ("GET", "/api/check"):
            return s.check_page(page(), **filt("q"))
        if route == ("GET", "/api/unanalysed"):
            return s.not_analysed_page(page(), **filt("q", "reason"))
        if route == ("GET", "/api/tags"):
            return s.tags_page(page(), **filt("q", "verdict", "problem"))
        if route == ("GET", "/api/tag-folders"):
            return s.tag_folders_page(page(), **filt("q", "verdict", "problem"))
        if route == ("POST", "/api/picard"):
            files = s.picard_files(body.get("folder", ""), body.get("k"))
            cmd = acoustid.picard_command(cfg.picard)
            if not cmd:
                return {"ok": False, "message": "MusicBrainz Picard wasn't found. " + acoustid.picard_help() +
                                                " Or set where it is in Settings."}
            subprocess.Popen(cmd + files, creationflags=_NO_WINDOW)
            return {"ok": True, "files": len(files)}
        if route == ("POST", "/api/decide"):
            s.decisions.set_many(body["files"], body.get("decision", ""))
            return {"ok": True, "marked": s.marked_summary()}
        if route == ("POST", "/api/bulk"):
            f = body.get("filters", {})
            f = {k: (v.lower() if k == "q" else v) for k, v in f.items()}
            pick = lambda *keys: {k: f.get(k, "") for k in (*keys, *COMMON_FILTERS)}
            if body["tab"] == "unanalysed" and body["action"] == "problems":
                n = s.mark_problem_files(**pick("q", "reason"))
            elif body["action"] == "suggested":
                n = s.mark_suggested(**pick("q", "status", "note", "marked", "fmt"))
            else:
                keys = ("q", "status", "note", "marked", "fmt") if body["tab"] == "identical" else ("q", "reason", "marked")
                n = s.clear_filtered(body["tab"], **pick(*keys))
            return {"changed": n, "marked": s.marked_summary()}

        # ---- scanning
        if route == ("POST", "/api/scan"):
            quick = bool(body.get("quick"))
            def job(progress, should_stop):
                res, _ = scan.run_scan(config.load(), progress, refresh=not quick, should_stop=should_stop)
                s.reload()
                return res.stats
            self.jobs.start("Quick re-sort" if quick else "Scan", job)
            return {"ok": True}
        if route == ("POST", "/api/damage"):
            folder = body.get("folder", "")
            def job(progress, should_stop):
                return {"checked": scan.run_damage_check(config.load(), folder, progress, should_stop)}
            self.jobs.start("Damage check", job)
            return {"ok": True}
        if route == ("POST", "/api/identify"):
            everything = bool(body.get("everything"))
            def job(progress, should_stop):
                try:
                    return scan.run_tag_check(config.load(), everything, progress, should_stop)
                finally:
                    s.reload_tags()          # what was found so far shows even when stopped
            self.jobs.start("Identifying tracks", job)
            return {"ok": True}
        if route == ("POST", "/api/stop"):
            self.jobs.stop_flag = True
            return {"ok": True}

        # ---- moving
        if route == ("GET", "/api/plan"):
            plan = s.plan()
            out = {"files": len(plan.ready), "bytes": plan.total, "problems": plan.problems,
                   "items": [{"src": i["src"], "dst": i["dst"], "size": i["size"]} for i in plan.ready[:300]],
                   "update_playlists": cfg.update_playlists, "playlist_edits": []}
            if cfg.update_playlists and plan.ready:
                repl = plexlists.replacements(s.move_rows(), {i["src"] for i in plan.ready})
                edits = plexlists.plan(plex.connect(cfg.plex_url, cfg.plex_token), cfg.sections, repl)
                out["playlist_edits"] = [{k: e[k] for k in ("kind", "container", "old_title", "new_title", "keeper_already_there")}
                                         for e in edits]
            return out
        if route == ("POST", "/api/move"):
            def job(progress, should_stop):
                c = config.load()
                plan = s.plan()     # planned again now, so it matches the decisions exactly
                rows = s.move_rows()
                result = mover.execute_moves(plan, config.sub_dir("moves"), c.holding_dir, progress)
                s.refresh_moved()
                if c.update_playlists and result["moved"]:
                    moved = {i["src"] for i in plan.ready} & s.moved
                    server = plex.connect(c.plex_url, c.plex_token)
                    progress("Finding the moved copies in playlists and collections", -1)
                    edits = plexlists.plan(server, c.sections, plexlists.replacements(rows, moved))
                    result["playlists"] = plexlists.apply(server, edits, self.playlist_log(plan.batch), progress)
                return result
            self.jobs.start("Move", job)
            return {"ok": True}
        if route == ("GET", "/api/batches"):
            out = []
            for b in mover.batches(config.sub_dir("moves"))[::-1]:
                log = plexlists.read_log(self.playlist_log(b["batch"]))
                b["playlist_edits"] = len(log)
                b["playlist_pending"] = plexlists.pending_count(self.playlist_log(b["batch"]), s.moved)
                out.append(b)
            return {"batches": out}
        if route == ("POST", "/api/undo-plan"):
            plan = mover.plan_undo(config.sub_dir("moves"), body["batch"], match=body.get("match", ""))
            files = {r["original"] for r in plan.ready}
            pl = [r for r in plexlists.read_log(self.playlist_log(body["batch"])) if r["file"] in files and not r.get("undone")]
            return {"files": len(plan.ready), "bytes": sum(r["size"] for r in plan.ready), "problems": plan.problems,
                    "items": [{"held": r["held"], "original": r["original"]} for r in plan.ready[:300]],
                    "playlist_entries": len(pl)}
        if route == ("POST", "/api/undo"):
            batch, match = body["batch"], body.get("match", "")
            def job(progress, should_stop):
                plan = mover.plan_undo(config.sub_dir("moves"), batch, match=match)
                result = mover.execute_undo(plan, progress)
                s.refresh_moved()
                restored = {r["original"] for r in plan.ready} - s.moved
                result["playlists"] = self.restore_playlists(batch, restored, progress)
                return result
            self.jobs.start("Undo", job)
            return {"ok": True}
        if route == ("POST", "/api/restore-playlists"):
            batch = body["batch"]
            def job(progress, should_stop):
                files = {r["file"] for r in plexlists.read_log(self.playlist_log(batch))} - s.moved
                return {"playlists": self.restore_playlists(batch, files, progress)}
            self.jobs.start("Restoring playlists", job)
            return {"ok": True}
        if route == ("POST", "/api/rescan-plex"):
            plex.request_rescan(plex.connect(cfg.plex_url, cfg.plex_token), cfg.sections)
            return {"ok": True}
        if route == ("POST", "/api/reveal"):
            path = s.results["tracks"][int(body["k"])]["file"]
            if body.get("folder"):     # the track's album folder, which is there even when the track isn't
                self.reveal(os.path.dirname(path), folder=True)
            else:
                self.reveal(path)
            return {"ok": True}
        if route == ("POST", "/api/open-reports"):
            self.reveal(config.sub_dir("reports"), folder=True)
            return {"ok": True}

        # ---- settings
        if route == ("GET", "/api/settings"):
            return {"rules": cfg.rules.__dict__, "holding_dir": cfg.holding_dir, "workers": cfg.workers,
                    "update_playlists": cfg.update_playlists,
                    "ffmpeg": cfg.ffmpeg, "fpcalc": cfg.fpcalc, "plex_scale": cfg.plex_scale,
                    "scale_measured": cfg.scale_measured, "default_holding": self.default_holding(),
                    "acoustid_key_set": bool(cfg.acoustid_key), "picard": cfg.picard,
                    "picard_found": " ".join(acoustid.picard_command(cfg.picard) or []),
                    "picard_help": acoustid.picard_help()}
        if route == ("POST", "/api/settings"):
            for k, v in body.get("rules", {}).items():
                if hasattr(cfg.rules, k):
                    setattr(cfg.rules, k, type(getattr(cfg.rules, k))(v))
            for k in ("holding_dir", "ffmpeg", "fpcalc", "picard"):
                if k in body:
                    setattr(cfg, k, str(body[k]).strip())
            # the key is never sent back to the page: an empty box leaves it as it is
            if str(body.get("acoustid_key") or "").strip():
                cfg.acoustid_key = str(body["acoustid_key"]).strip()
            if body.get("acoustid_key_remove"):
                cfg.acoustid_key = ""
            if "workers" in body:
                cfg.workers = max(1, min(32, int(body["workers"])))
            if "update_playlists" in body:
                cfg.update_playlists = bool(body["update_playlists"])
            config.save(cfg)
            return {"ok": True}

        # ---- setup
        if path.startswith("/api/setup/"):
            return self.setup_api(method, path[len("/api/setup/"):], q, body)
        raise KeyError(path)

    def playlist_log(self, batch):
        return os.path.join(config.sub_dir("moves"), "playlists", batch + ".jsonl")

    def restore_playlists(self, batch, files, progress):
        """Put back this batch's playlist entries for files that are back in the library."""
        log = self.playlist_log(batch)
        if not files or not plexlists.read_log(log):
            return {"restored": 0, "pending": [], "failed": []}
        c = config.load()
        server = plex.connect(c.plex_url, c.plex_token)
        by_file = {}

        def find_by_file(path):   # only needed when Plex forgot the old track; one track-list read
            if not by_file:
                progress("Looking up put-back files in Plex", -1)
                by_file.update({t["file"]: t["id"] for t in plex.fetch_tracks(server, c.sections, PathMapper(c.path_maps))})
            return by_file.get(path)
        return plexlists.undo(server, log, files, find_by_file, progress)

    def default_holding(self):
        r = self.state.results
        if r and r["tracks"]:
            return mover.holding_root_for(r["tracks"][0]["file"])
        return ""

    def reveal(self, path, folder=False):
        """Show a file (or folder) in Explorer / Finder / the file manager."""
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", path] if folder else ["explorer", "/select,", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path] if folder else ["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", path if folder else os.path.dirname(path)])

    # ---------------------------------------------------------------- setup steps

    def setup_api(self, method, step, q, body):
        cfg, st = self.state.cfg, self.setup
        if (method, step) == ("POST", "pin"):
            from plexapi.myplex import MyPlexPinLogin
            login = MyPlexPinLogin()
            st.update(pin=login.pin, token=None, servers=[], status="waiting")

            def wait():
                login.run(timeout=600)
                login.waitForLogin()
                if login.token:
                    st["token"] = login.token
                    try:
                        st["servers"] = plex.account_servers(login.token)
                        st["status"] = "signed in"
                    except Exception as e:
                        st["status"] = f"error: {e}"
                else:
                    st["status"] = "expired"
            threading.Thread(target=wait, daemon=True).start()
            return {"code": login.pin}
        if (method, step) == ("GET", "pin"):
            return {"status": st["status"], "servers": [{"name": x["name"], "owned": x["owned"]} for x in st["servers"]]}
        if (method, step) == ("POST", "server"):
            res = st["servers"][int(body["index"])]
            server, url, token = plex.connect_resource(res["resource"])
            cfg.plex_url, cfg.plex_token, cfg.server_name = url, token, res["name"]
            config.save(cfg)
            return {"ok": True, "server": cfg.server_name}
        if (method, step) == ("POST", "manual"):
            server = plex.connect(body["url"].strip(), body["token"].strip(), timeout=20)
            cfg.plex_url, cfg.plex_token, cfg.server_name = body["url"].strip(), body["token"].strip(), server.friendlyName
            config.save(cfg)
            return {"ok": True, "server": cfg.server_name}

        server = lambda: plex.connect(cfg.plex_url, cfg.plex_token, timeout=10)
        if (method, step) == ("GET", "status"):
            if not cfg.configured:
                return {"ok": False, "message": "Not connected yet."}
            try:
                return {"ok": True, "server": server().friendlyName}
            except Exception as e:
                return {"ok": False, "message": friendly_error(e, cfg)}
        if (method, step) == ("GET", "libraries"):
            return {"libraries": [{"key": k, "title": t, "folders": f} for k, t, f in plex.music_sections(server())],
                    "chosen": cfg.sections}
        if (method, step) == ("POST", "libraries"):
            cfg.sections = [str(k) for k in body.get("keys", [])]
            config.save(cfg)
            return {"ok": True}
        if (method, step) == ("GET", "paths"):
            samples = setup_wizard.sample_plex_paths(server(), cfg.sections)
            return {"example": samples[0] if samples else "", "found": share_found(PathMapper(cfg.path_maps), samples),
                    "maps": [m.__dict__ for m in cfg.path_maps]}
        if (method, step) == ("POST", "paths"):
            samples = setup_wizard.sample_plex_paths(server(), cfg.sections)
            local = body["local"].strip()
            m = next((suggest_map(p, local) for p in samples if suggest_map(p, local)), None)
            if not m:
                return {"ok": False, "message": "None of the sample tracks are in that folder. Try the folder that "
                                                "holds your artist folders, or the whole drive."}
            cfg.path_maps = [x for x in cfg.path_maps if x.plex != m.plex] + [m]
            config.save(cfg)
            return {"ok": True, "map": m.__dict__, "found": share_found(PathMapper(cfg.path_maps), samples)}
        if (method, step) == ("GET", "sonic"):
            return {"current": cfg.sonic_dir, "found_here": plexdata.auto_find() or ""}
        if (method, step) == ("POST", "sonic"):
            p = body.get("path", "").strip()
            if not p:
                cfg.sonic_dir = ""
                config.save(cfg)
                return {"ok": True, "sonic_dir": ""}
            hit = setup_wizard.check_sonic_dir(p)
            if not hit:
                return {"ok": False, "message": "No 'Music Analysis' folder with Track.tree and Mapping.db there. "
                                                "Point at the Plex data folder (the one called 'Plex Media Server')."}
            cfg.sonic_dir = hit
            config.save(cfg)
            return {"ok": True, "sonic_dir": hit}
        if (method, step) == ("POST", "measure"):
            def job(progress, should_stop):
                c = config.load()
                k = scan.measure_scale(c, progress=progress)
                if k:
                    c.plex_scale, c.scale_measured = round(k, 4), True
                    config.save(c)
                    self.state.cfg = c
                return {"scale": k}
            self.jobs.start("Measuring Plex's distance scale", job)
            return {"ok": True}
        raise KeyError(step)


def friendly_error(e: Exception, cfg=None) -> str:
    """Plain-language messages for the errors people actually hit."""
    name = type(e).__name__
    url = getattr(cfg, "plex_url", "") or "the server"
    if name in ("ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "NewConnectionError", "MaxRetryError"):
        return (f"Can't reach the Plex server at {url}. Check it's running and the address is right "
                f"(Setup, step 1).")
    if isinstance(e, acoustid.BadKey):
        return str(e)
    if name == "Unauthorized":
        return "Plex didn't accept the sign-in. Sign in again in Setup, step 1."
    if name == "FileNotFoundError":
        return f"File or folder not found: {e.filename or e}"
    if name == "PermissionError":
        return f"Not allowed to open or change: {e.filename or e}"
    return f"{name}: {e}"


def _clean(v):
    """JSON for browsers: no NaN or infinity (silence is measured as -infinity loudness)."""
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PlexSonicDoctor"

        def log_message(self, fmt, *args):   # keep the terminal quiet
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(_clean(body), default=str, allow_nan=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _local_host(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _authorised(self, q):
            given = self.headers.get("X-Sonic-Doctor") or q.get("t", "")
            return secrets.compare_digest(given, app.secret)

        def _handle(self, method):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if not self._local_host():
                return self._send(403, {"error": "local access only"})
            if not url.path.startswith("/api/"):
                return self._static(url.path)
            if not self._authorised(q):
                return self._send(403, {"error": "missing or wrong session key; reopen the app from its link"})
            if url.path == "/api/audio" and method == "GET":
                return self._audio(q)
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            try:
                self._send(200, app.api(method, url.path, q, body))
            except KeyError as e:
                self._send(404, {"error": f"unknown: {e}"})
            except Exception as e:
                traceback.print_exc()
                self._send(500, {"error": friendly_error(e, app.state.cfg)})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _static(self, path):
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC, name))
            if not full.startswith(STATIC) or not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            with open(full, "rb") as f:
                self._send(200, f.read(), TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

        def _audio(self, q):
            """A 30-second MP3 clip of one track from the current results, from the start or the middle."""
            r = app.state.results
            try:
                t = r["tracks"][int(q["k"])]
            except (TypeError, KeyError, IndexError, ValueError):
                return self._send(404, {"error": "no such track"})
            path = t["file"]
            if not os.path.exists(path):
                return self._send(404, {"error": "file not found (moved?)"})
            start = max(0.0, t["duration"] / 2 - 15) if q.get("at") == "middle" else 0.0
            cmd = [app.state.cfg.ffmpeg, "-v", "error", "-ss", f"{start:.1f}", "-t", "30", "-i", path,
                   "-map", "0:a:0", "-vn", "-ac", "2", "-ar", "44100", "-b:a", "160k", "-f", "mp3", "pipe:1"]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
            except OSError:
                return self._send(500, {"error": "ffmpeg isn't available"})
            data = proc.stdout.read()
            proc.wait()
            if not data:
                return self._send(500, {"error": "couldn't decode this file"})
            self._send(200, data, "audio/mpeg")

    return Handler


class _Server(ThreadingHTTPServer):
    # Windows lets a second program bind a port that's already listening when SO_REUSEADDR is
    # set (the http.server default), and then requests go to either one. Insist on our own port.
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32":
            import socket
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _instance_file():
    return os.path.join(config.data_dir(), "gui-instance.json")


def running_instance():
    """The link of a copy of the app that's already running for this user, if any."""
    import http.client
    try:
        with open(_instance_file(), encoding="utf-8") as f:
            info = json.load(f)
        c = http.client.HTTPConnection("127.0.0.1", int(info["port"]), timeout=3)
        c.request("GET", "/api/job", headers={"X-Sonic-Doctor": info["secret"], "Host": f"127.0.0.1:{info['port']}"})
        ok = c.getresponse().status == 200
        c.close()
        return f"http://127.0.0.1:{info['port']}/#t={info['secret']}" if ok else None
    except (OSError, ValueError, KeyError):
        return None


def main(port: int = 8765, open_browser: bool = True):
    existing = running_instance()
    if existing:
        # One copy at a time: two copies working on the same decisions would be confusing.
        print(f"Plex Sonic Doctor is already running:\n  {existing}", flush=True)
        if open_browser:
            webbrowser.open(existing)
        return
    app = App()
    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = _Server(("127.0.0.1", p), make_handler(app))
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        sys.exit("Couldn't find a free port for the app.")
    link = f"http://127.0.0.1:{port}/#t={app.secret}"
    with open(_instance_file(), "w", encoding="utf-8") as f:
        json.dump({"port": port, "secret": app.secret, "pid": os.getpid()}, f)
    if sys.platform != "win32":
        os.chmod(_instance_file(), 0o600)
    print(f"Plex Sonic Doctor is running at\n  {link}\nKeep this window open while you use it; close it (or press Ctrl+C) to stop.",
          flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(link)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            with open(_instance_file(), encoding="utf-8") as f:
                if json.load(f).get("pid") == os.getpid():
                    os.remove(_instance_file())
        except (OSError, ValueError):
            pass
