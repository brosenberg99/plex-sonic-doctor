"""The GUI's state and web server, on made-up results in a temp folder."""
import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

from helpers import fingerprint, noisy_copy


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="sonicdoctor_gui_")
        os.environ["SONICDOCTOR_HOME"] = os.path.join(cls.base, "home")
        from plex_sonic_doctor import analysis, config, reports
        from plex_sonic_doctor.config import Rules
        tracks, fps, streams = [], {}, {}
        a, b = fingerprint(), fingerprint()
        for i, (title, folder, codec, kbps, fp, dur) in enumerate([
                ("Song One", "A", "flac", 1000, a, 200), ("Song One", "B", "mp3", 320, noisy_copy(a), 200),
                ("Song Two", "A", "flac", 1000, b, 300), ("Song Two (Radio Edit)", "C", "mp3", 320, noisy_copy(b), 240)],
                start=1):
            path = os.path.join(cls.base, "Music", folder, f"{title}.{codec}")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(os.urandom(1000 + i))
            tracks.append({"id": i, "title": title, "artist": "X", "album": folder, "duration": float(dur), "codec": codec,
                           "bitrate": kbps, "size": 1000 + i, "file": path, "analysed": True})
            fps[path] = (fp, None, None)
            streams[i] = {"loudness": -12.0, "lra": 6.0, "peak": 0.9, "channels": 2.0}
        # Two tracks Plex couldn't analyse, in one album: a saved web page and its blocked album-mate.
        for i, (title, data) in enumerate([("Not A Song", b"<html><title>404</title></html>"),
                                           ("Fine Song", os.urandom(900))], start=5):
            path = os.path.join(cls.base, "Music", "D", f"{title}.mp3")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
            tracks.append({"id": i, "title": title, "artist": "Y", "album": "D", "album_id": 50, "duration": 180.0,
                           "codec": "mp3", "bitrate": 320, "size": len(data), "file": path, "analysed": False})
        checks = {tracks[4]["file"]: ("not audio", "it's a saved web page, not a song")}
        pairs = [(x, y, 0.01) for x in range(4) for y in range(x + 1, 4)]
        res = analysis.analyse(tracks, pairs, fps, streams, Rules(), lambda i, j: 0.01, checks=checks)
        cfg = config.load()
        cfg.holding_dir = os.path.join(cls.base, "holding")
        config.save(cfg)
        reports.write(res, streams, config.sub_dir("reports"))
        # AcoustID's made-up answers: Song One is what its tags say; "Not A Song" is something else.
        from plex_sonic_doctor import acoustid
        rec = lambda title, artist, album: {"score": 0.95, "id": "rec-" + title, "title": title, "artists": [artist],
                                            "albums": [{"id": "rg-" + album, "title": album, "type": "Album", "secondary": []}]}
        acoustid.write_report(tracks, {tracks[0]["file"]: [rec("Song One", "X", "A")],
                                       tracks[4]["file"]: [rec("Real Song", "Z", "Real Album")],
                                       tracks[5]["file"]: []}, config.sub_dir("reports"))
        cls.tracks = tracks

        from plex_sonic_doctor.gui.server import App, _Server, make_handler
        cls.app = App()
        cls.httpd = _Server(("127.0.0.1", 0), make_handler(cls.app))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        os.environ.pop("SONICDOCTOR_HOME", None)
        shutil.rmtree(cls.base, ignore_errors=True)

    def request(self, method, path, body=None, key=True, host="127.0.0.1"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": f"{host}:{self.port}"}
        if key:
            headers["X-Sonic-Doctor"] = self.app.secret if key is True else key
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        c.request(method, path, data, headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, raw

    def test_only_this_computer_with_the_key(self):
        self.assertEqual(self.request("GET", "/api/state", key=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/state", key="wrong")[0], 403)
        self.assertEqual(self.request("GET", "/api/state", host="evil.example")[0], 403)   # DNS-rebinding guard
        self.assertEqual(self.request("GET", "/api/state")[0], 200)

    def test_page_and_no_way_out_of_the_static_folder(self):
        status, body = self.request("GET", "/", key=False)
        self.assertEqual(status, 200)
        self.assertIn(b"Plex Sonic Doctor", body)
        self.assertEqual(self.request("GET", "/..%2F..%2Fconfig.py", key=False)[0], 404)
        self.assertEqual(self.request("GET", "/../server.py", key=False)[0], 404)

    def test_views_decisions_and_the_move_rules(self):
        st, body = self.request("GET", "/api/identical")
        groups = json.loads(body)["items"]
        self.assertEqual([[c["title"] for c in g["copies"]] for g in groups], [["Song One", "Song One"]])
        versions = json.loads(self.request("GET", "/api/versions?reason=length")[1])["items"]
        self.assertEqual(len(versions), 1)

        # mark suggested -> the MP3 copy; mark both versions of Song Two -> refused by the mover
        r = json.loads(self.request("POST", "/api/bulk", {"tab": "identical", "action": "suggested", "filters": {}})[1])
        self.assertEqual(r["changed"], 1)
        files = [v["file"] for v in versions[0]["versions"]]
        self.request("POST", "/api/decide", {"files": files, "decision": "remove"})
        plan = json.loads(self.request("GET", "/api/plan")[1])
        self.assertEqual(plan["files"], 1)
        self.assertTrue(plan["items"][0]["src"].endswith(os.path.join("B", "Song One.mp3")))
        self.assertIn("Song Two", plan["problems"][0])
        self.assertIn("every copy is marked", plan["problems"][0])
        r = json.loads(self.request("POST", "/api/bulk", {"tab": "versions", "action": "clear", "filters": {}})[1])
        self.assertEqual(r["changed"], 2)
        self.request("POST", "/api/bulk", {"tab": "identical", "action": "clear", "filters": {}})
        self.assertEqual(json.loads(self.request("GET", "/api/state")[1])["marked"]["files"], 0)

    def wait_for_job(self):
        import time
        for _ in range(200):
            job = json.loads(self.request("GET", "/api/job")[1])
            if not job["running"]:
                return job
            time.sleep(0.05)
        self.fail("job didn't finish")

    def stats(self):
        return json.loads(self.request("GET", "/api/state")[1])["results"]["stats"]

    def test_counts_follow_moves_and_undo(self):
        before = self.stats()
        self.assertEqual((before["identical_groups"], before["extra_copies"], before["broken_files"],
                          before["not_analysed"], before["tracks"]), (1, 1, 1, 2, 6))
        rows = json.loads(self.request("GET", "/api/unanalysed")[1])["items"]
        self.assertEqual([(r["title"], r["reason"], r["removable"]) for r in rows],
                         [("Not A Song", "problem file", True), ("Fine Song", "blocked by another file in its album", False)])
        self.assertIn("Not A Song.mp3", rows[1]["details"])

        # mark the extra copy and the problem file, then move them for real (in the temp folder)
        r = json.loads(self.request("POST", "/api/bulk", {"tab": "identical", "action": "suggested", "filters": {}})[1])
        r = json.loads(self.request("POST", "/api/bulk", {"tab": "unanalysed", "action": "problems", "filters": {}})[1])
        self.assertEqual((r["changed"], r["marked"]["files"]), (1, 2))
        self.assertEqual(json.loads(self.request("GET", "/api/plan")[1])["files"], 2)
        self.request("POST", "/api/move", {})
        job = self.wait_for_job()
        self.assertEqual(job["result"]["moved"], 2, job)
        after = self.stats()
        self.assertEqual((after["identical_groups"], after["extra_copies"], after["extra_bytes"], after["broken_files"],
                          after["not_analysed"], after["tracks"]), (0, 0, 0, 0, 1, 4))
        self.assertEqual(json.loads(self.request("GET", "/api/identical")[1])["total"], 0)   # done groups drop off
        self.assertEqual(json.loads(self.request("GET", "/api/broken")[1])["total"], 0)
        self.assertEqual(json.loads(self.request("GET", "/api/unanalysed")[1])["total"], 1)

        # undo puts everything back, and the counts with it
        batch = json.loads(self.request("GET", "/api/batches")[1])["batches"][0]["batch"]
        self.request("POST", "/api/undo", {"batch": batch})
        self.wait_for_job()
        self.assertEqual(self.stats(), before)
        self.request("POST", "/api/bulk", {"tab": "identical", "action": "clear", "filters": {}})
        self.request("POST", "/api/decide", {"files": [rows[0]["file"]], "decision": ""})

    def test_album_status_rules(self):
        from plex_sonic_doctor.gui.state import album_status
        ok = lambda f, new=False: {"file": f, "new": new, "problem": ""}
        bad = lambda f, new=False: {"file": f, "new": new, "problem": "unfinished iTunes download"}
        self.assertEqual(album_status({}, ["a"]), "todo")
        self.assertEqual(album_status({"files": [ok("a"), ok("b")], "gone": []}, ["a"]), "fixed")
        self.assertEqual(album_status({"files": [bad("a"), ok("b")], "gone": []}, ["a"]), "broken")
        self.assertEqual(album_status({"files": [bad("a"), ok("a 1", True)], "gone": []}, ["a"]), "replaced")
        self.assertEqual(album_status({"files": [bad("a"), bad("a 1", True)], "gone": []}, ["a"]), "broken")
        self.assertEqual(album_status({"files": [ok("b")], "gone": ["a"]}, ["a"]), "missing")
        self.assertEqual(album_status({"files": [ok("b"), ok("a 1", True)], "gone": ["a"]}, ["a"]), "fixed")
        self.assertEqual(album_status({"files": [ok("b")], "gone": ["c"]}, ["a"]), "fixed")   # c wasn't broken
        self.assertEqual(album_status({"files": [bad("a")], "skipped": True}, ["a"]), "skipped")

    def test_broken_by_album_and_recheck(self):
        albums = json.loads(self.request("GET", "/api/broken-albums")[1])
        self.assertEqual([(a["album"], a["status"], a["tracks"], len(a["broken"])) for a in albums["items"]],
                         [("D", "todo", 2, 1)])
        self.assertEqual(albums["status_counts"]["todo"], 1)
        folder = albums["items"][0]["folder"]
        self.assertEqual(json.loads(self.request("GET", "/api/broken-albums?status=fixed")[1])["total"], 0)

        self.request("POST", "/api/album-skip", {"folder": folder, "skipped": True})
        self.assertEqual(json.loads(self.request("GET", "/api/broken-albums")[1])["items"][0]["status"], "skipped")
        self.request("POST", "/api/album-skip", {"folder": folder, "skipped": False})
        if not shutil.which("ffmpeg"):
            return self.skipTest("ffmpeg not installed")

        page, fine = self.tracks[4]["file"], self.tracks[5]["file"]
        originals = {}
        for p in (page, fine):
            with open(p, "rb") as f:
                originals[p] = f.read()
        tone = lambda p: subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=1",
                                         "-c:a", "libmp3lame", "-f", "mp3", p], check=True)
        copy = os.path.join(folder, "Not A Song 1.mp3")
        try:
            def recheck():
                self.assertEqual(self.request("POST", "/api/recheck", {"folders": [folder, "/not/an/album"]})[0], 200)
                job = self.wait_for_job()
                self.assertEqual(job["result"]["rechecked"], 1, job)
                return json.loads(self.request("GET", "/api/broken-albums")[1])["items"][0]

            tone(fine)
            a = recheck()                    # the web page still isn't audio
            self.assertEqual(a["status"], "broken")
            self.assertEqual([(os.path.basename(f["file"]), f["problem"]) for f in a["found"]], [("Not A Song.mp3", "not audio")])

            tone(copy)                       # a fresh download beside it: mark the old one
            a = recheck()
            self.assertEqual(a["status"], "replaced")
            r = json.loads(self.request("POST", "/api/mark-old-copies", {"folder": folder})[1])
            self.assertEqual((r["changed"], r["marked"]["files"]), (1, 1))
            os.remove(copy)

            tone(page)                       # replaced where it was: fixed, and its Remove mark goes
            a = recheck()
            self.assertEqual((a["status"], a["checked_files"], a["broken"][0]["fixed"]), ("fixed", 2, True))
            self.assertEqual(json.loads(self.request("GET", "/api/state")[1])["marked"]["files"], 0)
            s = self.stats()
            self.assertEqual((s["broken_files"], s["broken_fixed"]), (0, 1))
            self.assertEqual(json.loads(self.request("GET", "/api/broken")[1])["total"], 0)
            self.assertEqual(json.loads(self.request("GET", "/api/plan")[1])["files"], 0)
        finally:
            for p, data in originals.items():
                with open(p, "wb") as f:
                    f.write(data)
            if os.path.exists(copy):
                os.remove(copy)
            self.app.state.album_checks.d.clear()
            self.app.state.album_checks.update(folder)          # saves the cleared checks
            self.request("POST", "/api/decide", {"files": [page], "decision": ""})

    def test_shared_filters_sorts_and_facets(self):
        get = lambda path: json.loads(self.request("GET", path)[1])
        titles = lambda d: [g["versions"][0]["title"] for g in d["items"]]
        self.assertEqual(get("/api/identical?folder=b")["total"], 1)          # any copy in folder B
        self.assertEqual(get("/api/identical?folder=c")["total"], 0)
        self.assertEqual(get("/api/identical?artist=x")["total"], 1)
        self.assertEqual(get("/api/identical?artist=nobody")["total"], 0)
        self.assertEqual(get("/api/identical?source=Bandcamp")["total"], 0)
        self.assertEqual(get("/api/identical?year_from=1990")["total"], 0)     # no years in these results
        self.assertEqual(get("/api/unanalysed?sort=title")["items"][0]["title"], "Fine Song")
        self.assertEqual(get("/api/unanalysed?sort=title&desc=1")["items"][0]["title"], "Not A Song")
        self.assertEqual(titles(get("/api/versions?sort=title")), ["Song Two"])
        self.assertEqual(get("/api/broken?folder=d")["total"], 1)
        self.assertEqual(get("/api/broken-albums?artist=y&sort=source")["total"], 1)
        f = get("/api/facets?tab=unanalysed")
        self.assertEqual((f["artists"], f["albums"], f["folders"]), (["Y"], ["D"], {"D": 2}))
        self.assertEqual(f["sources"], {"Not read yet": 2})
        # bulk actions take the shared filters too
        r = json.loads(self.request("POST", "/api/bulk", {"tab": "identical", "action": "suggested",
                                                          "filters": {"folder": "nowhere"}})[1])
        self.assertEqual(r["changed"], 0)

    def test_tags_tab(self):
        get = lambda path: json.loads(self.request("GET", path)[1])
        state = get("/api/state")
        self.assertEqual((state["tags"]["issues"], state["acoustid"]), (1, False))
        tracks = get("/api/tags")
        self.assertEqual(tracks["total"], 3)
        self.assertEqual(tracks["verdict_counts"]["Tags look right"], 2)
        issues = get("/api/tags?verdict=issues")["items"]
        self.assertEqual([(t["title"], t["verdict"], t["suggest"]["title"], t["suggest"]["album"]) for t in issues],
                         [("Not A Song", "Named differently", "Real Song", "Real Album")])
        self.assertEqual(get("/api/tags?q=real+song")["total"], 1)                 # identified names are searchable
        self.assertEqual(get("/api/tags?verdict=issues&folder=a")["total"], 0)     # shared filters
        folders = get("/api/tag-folders")["items"]                                 # folders with work to do
        self.assertEqual([(os.path.basename(f["folder"]), f["tracks"], f["counts"], f["guess"]["album"]) for f in folders],
                         [("D", 2, {"Named differently": 1}, "Real Album")])
        self.assertEqual(get("/api/facets?tab=tags")["artists"], ["X", "Y"])

        # Picard: only for folders of the library, and a clear message when it isn't installed
        self.assertEqual(self.request("POST", "/api/picard", {"folder": self.base})[0], 404)
        s, folder = self.app.state, folders[0]["folder"]
        self.assertEqual(s.picard_files(folder), [folder])
        self.assertEqual(s.picard_files(k=5), [self.tracks[5]["file"]])
        from unittest import mock
        with mock.patch("plex_sonic_doctor.gui.state.ALBUM_FOLDER_MAX_TRACKS", 1):     # a folder of loose tracks
            self.assertEqual(s.picard_files(folder), [self.tracks[4]["file"]])         # just its tracks to fix
        cfg = self.app.state.cfg
        old, cfg.picard = cfg.picard, os.path.join(self.base, "no-picard-here")
        try:
            r = json.loads(self.request("POST", "/api/picard", {"folder": folders[0]["folder"]})[1])
            self.assertFalse(r["ok"])
            self.assertIn("Picard wasn't found", r["message"])
        finally:
            cfg.picard = old

        # identifying needs a key first
        self.request("POST", "/api/identify", {})
        job = self.wait_for_job()
        self.assertIn("AcoustID key", job["error"])

    def test_top_folders(self):
        from plex_sonic_doctor.gui.state import LOOSE, top_folders
        ts = [{"file": "/m/iTunes/Media/a.m4a", "root": "/m"}, {"file": "/m/b.mp3", "root": "/m/"},
              {"file": "H:\\Music\\Artist\\Album\\c.mp3", "root": "H:\\Music"}]
        top_folders(ts)
        self.assertEqual([t["top"] for t in ts], ["iTunes", LOOSE, "Artist"])
        old = [{"file": os.path.join("x", "Music", "A", "1.mp3")}, {"file": os.path.join("x", "Music", "B", "2.mp3")}]
        top_folders(old)                   # no library folder saved: the shared folder stands in
        self.assertEqual([t["top"] for t in old], ["A", "B"])

    def test_a_second_copy_cant_share_the_port(self):
        from plex_sonic_doctor.gui.server import _Server, make_handler
        with self.assertRaises(OSError):
            _Server(("127.0.0.1", self.port), make_handler(self.app))

    def test_unknown_api_and_bad_track(self):
        self.assertEqual(self.request("GET", "/api/nope")[0], 404)
        self.assertEqual(self.request("GET", f"/api/audio?k=999&t={self.app.secret}", key=False)[0], 404)


if __name__ == "__main__":
    unittest.main()
