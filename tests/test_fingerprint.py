import os
import shutil
import subprocess
import tempfile
import unittest

import numpy as np

from plex_sonic_doctor import fingerprint
from helpers import fingerprint as fake_fp, noisy_copy


class MatchTests(unittest.TestCase):
    def test_match_levels(self):
        a = fake_fp()
        self.assertEqual(fingerprint.fp_match(a, a), 1.0)
        self.assertGreater(fingerprint.fp_match(a, noisy_copy(a, 0.03)), 0.95)
        self.assertLess(fingerprint.fp_match(a, fake_fp()), 0.6)
        self.assertGreater(fingerprint.fp_match(a[10:], a), 0.99)   # finds a 10-item offset
        self.assertIsNone(fingerprint.fp_match(a[:10], a))           # too short to judge

    def test_low_information(self):
        self.assertTrue(fingerprint.low_information(np.full(500, 3, dtype=np.uint32)))
        self.assertFalse(fingerprint.low_information(fake_fp()))


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class RealToolsTests(unittest.TestCase):
    """Makes a synthetic test tone with ffmpeg and fingerprints it with whatever is installed."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="sonicdoctor_fp_")
        cls.wav = os.path.join(cls.dir, "tone.wav")
        cls.mp3 = os.path.join(cls.dir, "tone.mp3")
        src = "aevalsrc='0.3*sin(2*PI*(220+40*sin(2*PI*0.3*t))*t)+0.2*sin(2*PI*(330+90*sin(2*PI*0.11*t))*t)':s=44100:d=300"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-ac", "2", cls.wav], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", cls.wav, "-b:a", "128k", cls.mp3], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def backends(self):
        out = []
        if fingerprint.ffmpeg_has_chromaprint("ffmpeg"):
            out.append(fingerprint.Backend("ffmpeg", "ffmpeg", None))
        if shutil.which("fpcalc"):
            out.append(fingerprint.Backend("fpcalc", "ffmpeg", "fpcalc"))
        if not out:
            self.skipTest("no Chromaprint available")
        return out

    def test_wav_and_mp3_of_the_same_audio_match(self):
        for b in self.backends():
            _, _, _, s1, m1, e1 = fingerprint.fingerprint_file(b, self.wav, 300)
            _, _, _, s2, m2, e2 = fingerprint.fingerprint_file(b, self.mp3, 300)
            self.assertIsNone(e1); self.assertIsNone(e2)
            a, c = np.frombuffer(s1, "<u4"), np.frombuffer(s2, "<u4")
            self.assertGreater(fingerprint.fp_match(a, c), 0.85, b.kind)
            self.assertIsNotNone(m1, b.kind)

    def test_both_methods_agree(self):
        bs = self.backends()
        if len(bs) < 2:
            self.skipTest("only one fingerprint method installed")
        fa = fingerprint.fingerprint_file(bs[0], self.mp3, 300)
        fb = fingerprint.fingerprint_file(bs[1], self.mp3, 300)
        self.assertGreater(fingerprint.fp_match(np.frombuffer(fa[3], "<u4"), np.frombuffer(fb[3], "<u4")), 0.98)
        self.assertGreater(fingerprint.fp_match(np.frombuffer(fa[4], "<u4"), np.frombuffer(fb[4], "<u4")), 0.98)

    def test_damage_check(self):
        broken = os.path.join(self.dir, "broken.mp3")
        with open(self.mp3, "rb") as f:
            data = f.read()
        with open(broken, "wb") as f:
            f.write(data[:len(data) // 3] + os.urandom(20000) + data[len(data) // 2:])
        self.assertEqual(fingerprint.damage_check_file("ffmpeg", self.mp3)[3], 0)
        self.assertGreater(fingerprint.damage_check_file("ffmpeg", broken)[3], 0)


if __name__ == "__main__":
    unittest.main()
