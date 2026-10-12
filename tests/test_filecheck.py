"""The file check for tracks Plex couldn't analyse, on small made-up files in a temp folder."""
import os
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import unittest

from plex_sonic_doctor import filecheck, fingerprint


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def m4a(entry: bytes, sinf_children: bytes = None) -> bytes:
    """A minimal MP4 laid out like an iTunes download: ftyp, moov/trak/.../stsd/<entry>, mdat."""
    sample = b"\0" * 28 + box(b"esds", b"\0" * 20)
    if sinf_children is not None:
        sample += box(b"sinf", sinf_children)
    stsd = box(b"stsd", b"\0\0\0\0" + struct.pack(">I", 1) + box(entry, sample))
    moov = box(b"moov", box(b"trak", box(b"mdia", box(b"minf", box(b"stbl", stsd)))))
    return box(b"ftyp", b"M4A \0\0\0\0") + moov + box(b"mdat", os.urandom(2000))


UNFINISHED = box(b"frma", b"mp4a") + box(b"schm", b"\0\0\0\0itun\0\0\0\0") + box(b"free", b"\0" * 64)
PROTECTED = box(b"frma", b"mp4a") + box(b"schm", b"\0\0\0\0itun\0\0\0\0") + box(b"schi", box(b"user", b"\0\0\0\1"))


class FileCheckTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sonicdoctor_check_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, data):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_web_pages_and_empty_files_are_not_audio(self):
        page = self.write("03. Song.mp3", b'<?xml version="1.0"?>\n<!DOCTYPE html>\n<html><head><title>404 - Not Found'
                                          b'</title></head><body><h1>404 - Not Found</h1></body></html>\n')
        problem, details = filecheck.check_file("ffmpeg", page)
        self.assertEqual(problem, "not audio")
        self.assertIn("404 - Not Found", details)
        self.assertEqual(filecheck.check_file("ffmpeg", self.write("empty.mp3", b""))[0], "empty file")
        self.assertEqual(filecheck.check_file("ffmpeg", os.path.join(self.dir, "gone.mp3"))[0], "missing from disk")

    def test_unfinished_itunes_download_vs_protected_vs_plain(self):
        self.assertEqual(filecheck.mp4_protection(self.write("a.m4a", m4a(b"drms", UNFINISHED))), filecheck.UNFINISHED_ITUNES)
        self.assertEqual(filecheck.mp4_protection(self.write("b.m4a", m4a(b"drms", PROTECTED))), filecheck.DRM)
        self.assertEqual(filecheck.mp4_protection(self.write("c.m4a", m4a(b"enca", b""))), filecheck.OTHER_DRM)
        self.assertIsNone(filecheck.mp4_protection(self.write("d.m4a", m4a(b"mp4a"))))
        # check_file reports it without needing ffmpeg
        self.assertEqual(filecheck.check_file("no-such-ffmpeg", self.write("e.m4a", m4a(b"drms", UNFINISHED)))[0],
                         "unfinished iTunes download")

    def test_results_are_saved_and_a_failed_check_is_retried(self):
        conn = fingerprint.open_cache(os.path.join(self.dir, "cache.sqlite3"))
        page = self.write("page.mp3", b"<html><title>Oops</title></html>")
        other = self.write("other.mp3", b"\xff\xfb" + os.urandom(500))
        tracks = [{"file": page}, {"file": other}]
        self.assertEqual(filecheck.run_file_checks(conn, "no-such-ffmpeg", tracks, workers=2), 2)
        self.assertEqual(filecheck.load_file_checks(conn), {page: ("not audio", "it's a saved web page (“Oops”), not a song")})
        # unchanged page isn't looked at again; the file ffmpeg couldn't check (no ffmpeg) is
        self.assertEqual(filecheck.run_file_checks(conn, "no-such-ffmpeg", tracks, workers=2), 1)
        conn.close()


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class RealDecodeTests(unittest.TestCase):
    def test_real_audio_passes_and_garbage_fails(self):
        d = tempfile.mkdtemp(prefix="sonicdoctor_check_")
        try:
            good = os.path.join(d, "tone.m4a")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=3", "-c:a", "aac", good],
                           check=True)
            self.assertIsNone(filecheck.check_file("ffmpeg", good))
            bad = os.path.join(d, "bad.mp3")
            with open(bad, "wb") as f:
                f.write(os.urandom(4000))
            self.assertEqual(filecheck.check_file("ffmpeg", bad)[0], "couldn't be decoded")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
