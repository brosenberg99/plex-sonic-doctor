"""Tags checked against AcoustID: the fingerprint format, the verdicts, the cached lookups and the
report. No network: AcoustID's answers are made up."""
import base64
import csv
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from helpers import fingerprint as random_fp, track

from plex_sonic_doctor import acoustid, fingerprint
from plex_sonic_doctor.acoustid import DISAGREE, LOOKS_RIGHT, MISSING, NOT_FOUND, UNSURE


def decompress(text):
    """The reverse of acoustid.compress, written from Chromaprint's format description."""
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    n = int.from_bytes(data[1:4], "big")
    body = int.from_bytes(data[4:], "little")
    normal, i, zeros = [], 0, 0
    while zeros < n:
        v = (body >> (3 * i)) & 7
        i += 1
        normal.append(v)
        zeros += v == 0
    exceptional = body >> (((3 * i + 7) // 8) * 8)
    out, last, value, bit, j = [], 0, 0, 0, 0
    for v in normal:
        if v == 0:
            last ^= value
            out.append(last)
            value = bit = 0
            continue
        if v == 7:
            v += (exceptional >> (5 * j)) & 31
            j += 1
        bit += v
        value |= 1 << (bit - 1)
    return data[0], out


def match(title, artist, score=0.95, rid=None, albums=(("rg-1", "The Album", "Album", ()),)):
    return {"score": score, "id": rid or f"rec-{title}", "title": title, "artists": artist.split(" & "),
            "albums": [{"id": a[0], "title": a[1], "type": a[2], "secondary": list(a[3])} for a in albums]}


def answer(*recordings, score=0.95):
    """An AcoustID lookup answer with these recordings in one result."""
    return {"status": "ok", "results": [{"id": "r1", "score": score, "recordings": [
        {"id": r["id"], "title": r["title"], "artists": [{"name": a} for a in r["artists"]],
         "releasegroups": [{"id": a["id"], "title": a["title"], "type": a["type"], "secondarytypes": a["secondary"]}
                           for a in r["albums"]]} for r in recordings]}]}


class CompressTests(unittest.TestCase):
    def test_round_trip(self):
        for fp in (random_fp(948), random_fp(3), [0, 0xFFFFFFFF, 0x80000001, 0]):
            algorithm, back = decompress(acoustid.compress(fp))
            self.assertEqual(algorithm, 1)
            self.assertEqual(back, [int(x) for x in fp])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("fpcalc"), "ffmpeg and fpcalc not installed")
    def test_same_text_as_fpcalc(self):
        d = tempfile.mkdtemp(prefix="sonicdoctor_acoustid_")
        try:
            path = os.path.join(d, "noise.wav")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=d=20:c=pink:seed=3",
                            "-f", "lavfi", "-i", "sine=f=330:d=20", "-filter_complex", "amix=inputs=2", path], check=True)
            run = lambda *a: json.loads(subprocess.run(["fpcalc", "-json", *a, path], capture_output=True, check=True).stdout)
            raw, text = run("-raw")["fingerprint"], run()["fingerprint"]
            self.assertGreater(len(raw), 100)
            self.assertEqual(acoustid.compress(raw), text)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class VerdictTests(unittest.TestCase):
    def test_tags_that_look_wrong(self):
        problems = lambda **k: acoustid.tag_problems(dict(track(1, "Song"), **k))
        self.assertEqual(problems(), [])
        self.assertEqual(problems(artist="[Unknown Artist]", album="[Unknown Album]"),
                         [acoustid.NO_ARTIST, acoustid.NO_ALBUM])
        self.assertEqual(problems(title="Track 07"), [acoustid.NO_TITLE])
        self.assertEqual(problems(title="03 - Some Song"), [acoustid.FILE_NAME_TITLE])
        self.assertEqual(problems(title="some_song.mp3"), [acoustid.FILE_NAME_TITLE])
        self.assertEqual(problems(title="Song (Official Music Video)"), [acoustid.VIDEO_TITLE])
        # real names that only look odd
        for title in ("1999", "7", "No", "Untitled", "Superunknown"):
            self.assertEqual(problems(title=title), [], title)
        self.assertEqual(problems(artist="Unknown Mortal Orchestra", album="Superunknown"), [])

    def test_verdicts(self):
        t = lambda title="Song", artist="Artist", album="Album": track(1, title, artist, album)
        right = [match("Song", "Artist")]
        self.assertEqual(acoustid.judge(t(), right)[0], LOOKS_RIGHT)
        self.assertEqual(acoustid.judge(t(album="[Unknown Album]"), right)[0], MISSING)
        self.assertEqual(acoustid.judge(t("[Unknown]", "[Unknown Artist]", "[Unknown Album]"), right)[0], MISSING)
        self.assertEqual(acoustid.judge(t("Other Song"), right)[0], DISAGREE)
        # the right song under messy tags (seen in a real library)
        self.assertEqual(acoustid.judge(t("Muse - Apocalypse Please (Official Audio)", "Muse"),
                                        [match("Apocalypse Please", "Muse")])[0], MISSING)
        self.assertEqual(acoustid.judge(t("Final Fantasy 6 - Forever Rachel", "Final Fantasy 6"),
                                        [match("FFVI - Forever Rachel", "Fantasy Reborn")])[0], MISSING)
        self.assertEqual(acoustid.judge(t("37 - Sky Pillar (Score Demo)", "Justin E. Bell"),
                                        [match("Sky Pillar (score demo)", "Justin Bell")])[0], MISSING)
        self.assertEqual(acoustid.judge(t("Blitz Off!", "Final Fantasy X"), [match("Blitz Off", "Masashi Hamauzu")])[0],
                         MISSING)                                    # a game's name as the artist
        self.assertEqual(acoustid.judge(t("Lothlorien"), [match("Lothlórien / Lament for Gandalf", "Howard Shore")])[0],
                         MISSING)
        self.assertEqual(acoustid.judge(t("Sims - Buy 2", "Sims"), [match("Groceries", "Marc Russo")])[0], DISAGREE)
        # no artist, but a real title that isn't the match's: named differently; a bare number says nothing
        self.assertEqual(acoustid.judge(t("Game Over", "[Unknown Artist]"), [match("Overture", "Someone")])[0], DISAGREE)
        self.assertEqual(acoustid.judge(t("052", "[Unknown Artist]"), [match("Reach You", "Someone")])[0], MISSING)
        # empty tags and AcoustID can't decide between two recordings
        rivals = [match("Song", "Artist"), match("Different", "Someone Else")]
        self.assertEqual(acoustid.judge(t("Track 01", "[Unknown Artist]"), rivals)[0], UNSURE)
        # nothing found, or only a weak match: says something only about tags that already look wrong
        weak = [match("Other Song", "Artist", score=0.6)]
        self.assertEqual(acoustid.judge(t(), [])[0], LOOKS_RIGHT)
        self.assertEqual(acoustid.judge(t(), weak)[0], LOOKS_RIGHT)
        self.assertEqual(acoustid.judge(t(album="[Unknown Album]"), [])[0], NOT_FOUND)
        self.assertEqual(acoustid.judge(t(album="[Unknown Album]"), weak)[0], UNSURE)
        # a weak match the tags agree with still counts
        self.assertEqual(acoustid.judge(t(), [match("Song", "Artist", score=0.6)])[0], LOOKS_RIGHT)

    def test_names_that_are_the_same(self):
        same = acoustid.same_name
        self.assertTrue(same("No Se Que Hacer", "No sé que hacer"))
        self.assertTrue(same("Song (Remastered 2011)", "Song"))
        self.assertTrue(same("Song feat. Someone", "Song"))
        self.assertTrue(same("Sonata for Piano No. 10 in C Major, K. 330: III. Allegretto",
                             "Sonata for Piano No. 10 in C major, K. 300h/330: III. Allegretto"))
        self.assertFalse(same("Song", "Song 2"))
        self.assertFalse(same("", ""))
        m = match("Song", "Daft Punk & Pharrell Williams")
        self.assertTrue(acoustid.artist_agrees(track(1, "Song", "Daft Punk feat. Pharrell Williams"), m))
        self.assertTrue(acoustid.artist_agrees(track(1, "Song", "The Daft Punk"), m))
        self.assertFalse(acoustid.artist_agrees(track(1, "Song", "Someone"), m))

    def test_album_choice(self):
        m = match("Song", "Artist", albums=[("c", "Greatest Hits", "Album", ["Compilation"]), ("s", "Song", "Single", []),
                                            ("a", "The Album", "Album", [])])
        self.assertEqual(acoustid.best_album(m)["id"], "a")
        self.assertEqual(acoustid.best_album(m, "Greatest Hits")["id"], "c")   # the tags already name one of them


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sonicdoctor_acoustid_")
        self.conn = fingerprint.open_cache(os.path.join(self.dir, "cache.sqlite3"))
        self.tracks = [track(i, f"Song {i}", folder=self.dir) for i in range(1, 4)]
        for t in self.tracks:
            self.conn.execute("insert into fp values (?,?,?,?,?,?)", (t["file"], 100, 1.0, random_fp().tobytes(), None, None))
        self.calls, self.slept = [], []
        self.now = 0.0

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_lookups(self, reply):
        def fetch(key, fp, duration):
            self.calls.append((key, fp, duration))
            r = reply(len(self.calls))
            if isinstance(r, Exception):
                raise r
            return r

        def sleep(s):
            self.slept.append(s)
            self.now += s

        def clock():
            self.now += 0.01
            return self.now
        return acoustid.ensure_lookups(self.conn, "KEY", self.tracks, fetch=fetch, sleep=sleep, clock=clock)

    def test_looked_up_once_and_again_when_the_file_changes(self):
        ok = lambda n: answer(match("Song", "Artist"))
        self.assertEqual(self.run_lookups(ok), 3)
        key, fp, duration = self.calls[0]
        self.assertEqual((key, duration), ("KEY", 200.0))
        self.assertTrue(fp.startswith("AQAD"))                     # algorithm 1, 948 items
        self.assertTrue(all(0 < s <= acoustid.MIN_INTERVAL for s in self.slept))   # at most 3 a second
        self.assertEqual(self.run_lookups(ok), 0)
        self.conn.execute("update fp set mtime = 2.0 where file = ?", (self.tracks[0]["file"],))
        self.assertEqual(self.run_lookups(ok), 1)
        found = acoustid.load_lookups(self.conn)
        self.assertEqual(sorted(found), sorted(t["file"] for t in self.tracks))
        self.assertEqual(found[self.tracks[0]["file"]][0]["title"], "Song")

    def test_network_trouble_is_tried_again_and_a_bad_key_stops(self):
        flaky = lambda n: OSError("timed out") if n == 2 else answer(match("Song", "Artist"))
        self.assertEqual(self.run_lookups(flaky), 2)
        self.assertEqual(self.run_lookups(lambda n: answer()), 1)          # the one that failed
        self.conn.execute("delete from acoustid")
        bad = lambda n: {"status": "error", "error": {"code": 4, "message": "invalid API key"}}
        with self.assertRaises(acoustid.BadKey):
            self.run_lookups(bad)
        self.assertEqual(acoustid.load_lookups(self.conn), {})
        self.assertEqual(self.run_lookups(lambda n: OSError("no route to host")), 0)
        self.tracks += [track(i, f"Song {i}", folder=self.dir) for i in range(4, 4 + acoustid.GIVE_UP_AFTER)]
        for t in self.tracks[3:]:
            self.conn.execute("insert into fp values (?,?,?,?,?,?)", (t["file"], 100, 1.0, random_fp().tobytes(), None, None))
        with self.assertRaises(RuntimeError):                               # AcoustID down: give up, keep what's done
            self.run_lookups(lambda n: OSError("no route to host"))

    def test_silence_isnt_looked_up(self):
        self.conn.execute("update fp set start = ? where file = ?", (bytes(4 * 948), self.tracks[0]["file"]))
        self.assertEqual(self.run_lookups(lambda n: answer()), 2)

    def test_report(self):
        T = self.tracks
        T[1]["album"] = "[Unknown Album]"
        T[2]["title"] = "Track 3"
        T[2]["artist"] = "[Unknown Artist]"
        lookups = {T[0]["file"]: [match("Song 1", "Artist")],                    # right
                   T[1]["file"]: [match("Song 2", "Artist", albums=[("rg-9", "Real Album", "Album", [])])],
                   T[2]["file"]: []}                                              # not found
        data = acoustid.write_report(T, lookups, self.dir)
        self.assertEqual({k: v for k, v in data["stats"]["verdicts"].items() if v},
                         {LOOKS_RIGHT: 1, MISSING: 1, NOT_FOUND: 1})
        self.assertEqual((data["stats"]["suspect_tracks"], data["stats"]["suspect_looked_up"]), (2, 2))
        with open(os.path.join(self.dir, "tag_issues.csv"), encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r["verdict"], r["identified_album"]) for r in rows], [(MISSING, "Real Album"), (NOT_FOUND, "")])
        self.assertTrue(rows[0]["musicbrainz_recording"].startswith("https://musicbrainz.org/recording/"))
        with open(os.path.join(self.dir, "tags.json"), encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["rows"]), 3)
        self.assertIn("Identified, needs tags: 1", acoustid.summary_text(data["stats"]))

    def test_a_folders_album(self):
        from plex_sonic_doctor.gui.state import folder_album
        row = lambda *albums, verdict=MISSING: {"verdict": verdict, "artist": "Artist", "albums": [list(a) for a in albums]}
        rows = [row(("a", "The Album"), ("s", "Single")), row(("a", "The Album"), ("c", "Hits")), row(("x", "Other"))]
        self.assertEqual(folder_album(rows), {"album": "The Album", "artist": "Artist", "release_group": "a",
                                              "tracks": 2, "of": 3})
        self.assertIsNone(folder_album([row(("a", "A")), row(("b", "B")), row(("c", "C"))]))
        self.assertIsNone(folder_album([row(("a", "A"), verdict=UNSURE)]))


if __name__ == "__main__":
    unittest.main()
