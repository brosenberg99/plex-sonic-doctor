"""Where files came from, on small tagged files in a temp folder."""
import os
import shutil
import subprocess
import tempfile
import unittest

from mutagen.id3 import COMM, ID3, PRIV, TENC, TIT2

from plex_sonic_doctor import fingerprint, sources


def mp3(path, *frames):
    with open(path, "wb") as f:
        f.write(b"\xff\xfb\x90\x00" + b"\0" * 400)
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Song"))
    for fr in frames:
        tags.add(fr)
    tags.save(path)
    return path


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sonicdoctor_sources_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, *parts):
        p = os.path.join(self.dir, *parts)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p

    def test_marks_in_tags(self):
        self.assertEqual(sources.classify(mp3(self.path("a.mp3"), COMM(encoding=3, lang="eng", desc="",
                                                                        text="Visit https://someone.bandcamp.com"))), "Bandcamp")
        self.assertEqual(sources.classify(mp3(self.path("b.mp3"), COMM(encoding=3, lang="eng", desc="",
                                                                        text="Amazon.com Song ID: 2000003"))), "Amazon")
        self.assertEqual(sources.classify(mp3(self.path("c.mp3"), PRIV(owner="www.amazon.com", data=b"<uits/>"))), "Amazon")
        self.assertEqual(sources.classify(mp3(self.path("d.mp3"), TENC(encoding=3, text="dBpoweramp Release 14.4"))), "CD rip")
        self.assertEqual(sources.classify(mp3(self.path("e.mp3"))), "Unknown")

    def test_the_folder_is_the_fallback(self):
        self.assertEqual(sources.classify(mp3(self.path("Bandcamp", "x.mp3"))), "Bandcamp")
        self.assertEqual(sources.classify(mp3(self.path("Amazon MP3", "y.mp3"), TENC(encoding=3, text="LAME"))), "Amazon")
        with open(self.path("SoundCloud", "z.mp3"), "wb") as f:   # not even readable
            f.write(os.urandom(300))
        self.assertEqual(sources.classify(self.path("SoundCloud", "z.mp3")), "SoundCloud")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_itunes_purchase(self):
        from mutagen.mp4 import MP4
        p = self.path("bought.m4a")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=1", "-c:a", "aac", p], check=True)
        self.assertEqual(sources.classify(p), "Unknown")
        m = MP4(p)
        m["purd"] = ["2014-03-01 10:00:00"]
        m.save()
        self.assertEqual(sources.classify(p), "iTunes Store")

    def test_cached_and_only_changed_files_read_again(self):
        conn = fingerprint.open_cache(os.path.join(self.dir, "cache.sqlite3"))
        a = mp3(self.path("a.mp3"), COMM(encoding=3, lang="eng", desc="", text="https://x.bandcamp.com"))
        b = mp3(self.path("b.mp3"))
        self.assertEqual(sources.ensure_sources(conn, [a, b], workers=2), 2)
        self.assertEqual(sources.load_sources(conn), {a: "Bandcamp", b: "Unknown"})
        self.assertEqual(sources.ensure_sources(conn, [a, b], workers=2), 0)
        mp3(b, COMM(encoding=3, lang="eng", desc="", text="https://youtube.com/watch?v=1"))
        os.utime(b, (1, 1))
        self.assertEqual(sources.ensure_sources(conn, [a, b], workers=2, recheck=False), 0)   # quick: cache as is
        self.assertEqual(sources.ensure_sources(conn, [a, b], workers=2), 1)
        self.assertEqual(sources.load_sources(conn, [b]), {b: "YouTube"})
        conn.close()


if __name__ == "__main__":
    unittest.main()
