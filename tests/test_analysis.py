import unittest

import numpy as np

from plex_sonic_doctor import analysis
from plex_sonic_doctor.config import Rules
from helpers import fingerprint, noisy_copy, stream, track


def run(tracks, fps, streams, pairs=None, rules=None, distances=None):
    """Every pair of tracks is offered as a close sonic pair unless pairs are given."""
    if pairs is None:
        pairs = [(a, b, 0.02) for a in range(len(tracks)) for b in range(a + 1, len(tracks))]
    distances = distances or {}
    return analysis.analyse(tracks, pairs, {tracks[k]["file"]: v for k, v in fps.items()}, streams,
                            rules or Rules(), lambda a, b: distances.get((min(a, b), max(a, b))))


def groups(res):
    return [[res.tracks[c["k"]]["id"] for c in g["copies"]] for g in res.identical]


class IdenticalAndVersions(unittest.TestCase):
    def test_identical_copies_keep_the_lossless_one(self):
        a = fingerprint()
        tracks = [track(1, "Song", codec="mp3", bitrate=128), track(2, "Song", codec="flac", bitrate=1000),
                  track(3, "Song (1)", codec="mp3", bitrate=128)]
        fps = {0: (noisy_copy(a), None, None), 1: (a, None, None), 2: (noisy_copy(a), None, None)}
        res = run(tracks, fps, {i: stream() for i in (1, 2, 3)})
        self.assertEqual(groups(res), [[2, 1, 3]])
        self.assertEqual(res.identical[0]["status"], "Likely identical")
        self.assertEqual(res.versions, [])

    def test_radio_edit_remaster_and_mono_are_versions(self):
        a = fingerprint()
        tracks = [track(1, "Song", duration=300), track(2, "Song", duration=240),           # edit: shorter
                  track(3, "Song", duration=300.5), track(4, "Song", duration=300.2)]       # remaster, mono
        fps = {k: (noisy_copy(a), None, None) for k in range(4)}
        streams = {1: stream(-12), 2: stream(-12), 3: stream(-9.0), 4: stream(-12, channels=1.0)}
        res = run(tracks, fps, streams)
        self.assertEqual(groups(res), [])
        reasons = [v["reasons"] for v in res.versions[0]["versions"][1:]]
        flat = " | ".join("; ".join(r) for r in reasons)
        self.assertIn("length differs by 1:00", flat)
        self.assertIn("loudness differs by 3.0 LU", flat)
        self.assertIn("mono vs stereo", flat)

    def test_length_rule_is_a_setting(self):
        a = fingerprint()
        tracks = [track(1, "Song", duration=200), track(2, "Song", duration=202.5)]
        fps = {0: (a, None, None), 1: (noisy_copy(a), None, None)}
        streams = {1: stream(), 2: stream()}
        self.assertEqual(groups(run(tracks, fps, streams)), [])                           # 2 s default
        grouped = groups(run(tracks, fps, streams, rules=Rules(identical_max_length_diff=3.0)))
        self.assertEqual([sorted(g) for g in grouped], [[1, 2]])

    def test_title_words(self):
        a = fingerprint()
        tracks = [track(1, "Paper Moon"), track(2, "Paper Moon (Live)")]
        fps = {0: (a, None, None), 1: (noisy_copy(a), None, None)}
        streams = {1: stream(), 2: stream()}
        res = run(tracks, fps, streams)
        self.assertEqual(groups(res), [])
        self.assertIn("title says", res.versions[0]["versions"][1]["reasons"][0])
        res = run(tracks, fps, streams, rules=Rules(split_on_title_words=False))
        self.assertEqual(len(groups(res)), 1)
        self.assertIn("title says", res.identical[0]["note"])

    def test_version_words_only_in_brackets_and_only_for_the_same_song(self):
        self.assertEqual(analysis.version_words("Live at Heart"), set())
        self.assertEqual(analysis.version_words("Paper Moon (Radio Edit)"), {"edit"})
        self.assertEqual(analysis.version_words("Song - Original Mix"), set())
        self.assertEqual(analysis.version_words("Night Drive (Alternative)"), set())
        self.assertIsNone(analysis.title_word_difference({"title": "Night Drive (Karaoke)"}, {"title": "050"}))

    def test_different_audio_and_silence_are_not_grouped(self):
        tracks = [track(1, "A"), track(2, "B"), track(3, "Quiet"), track(4, "Quiet 2")]
        silence = np.full(948, 7, dtype=np.uint32)
        fps = {0: (fingerprint(), None, None), 1: (fingerprint(), None, None),
               2: (silence, None, None), 3: (silence.copy(), None, None)}
        res = run(tracks, fps, {i: stream() for i in range(1, 5)})
        self.assertEqual(groups(res), [])
        self.assertEqual(res.stats["verdicts"]["different audio"], 1)

    def test_sonic_only_when_a_file_cant_be_fingerprinted(self):
        tracks = [track(1, "Song"), track(2, "Song")]
        res = run(tracks, {}, {1: stream(), 2: stream()}, pairs=[(0, 1, 0.004)], distances={(0, 1): 0.004})
        self.assertEqual(groups(res), [[1, 2]])
        self.assertTrue(res.identical[0]["status"].startswith("Sonic match only"))


class Broken(unittest.TestCase):
    def test_per_file_problems(self):
        t = track(1, "Paper Moon")
        self.assertEqual(analysis.track_problems(t, stream(80.7, 0, 99990.0), None)[0][:2],
                         ("Broken", "impossible audio levels"))
        self.assertEqual(analysis.track_problems(t, stream(float("-inf"), 0, 0), None)[0][:2],
                         ("Likely broken", "silent"))
        self.assertEqual(analysis.track_problems(track(2, "Silence (Interlude)"), stream(float("-inf"), 0, 0), None)[0][0],
                         "Check")
        self.assertEqual(analysis.track_problems(t, stream(1.2, 0.03, 1.0), None)[0][1], "constant full-volume noise")
        self.assertEqual(analysis.track_problems(t, stream(), (None, None, "RuntimeError: moov atom not found"))[0][0],
                         "Broken")
        self.assertEqual(analysis.track_problems(t, stream(), (None, None, "FileNotFoundError: x"))[0][1],
                         "missing from disk")
        short = np.zeros(40 * 8, dtype=np.uint32)   # ~40 s heard of a 200 s track
        self.assertEqual(analysis.track_problems(t, stream(), (short, None, None))[0][1], "audio ends early")
        self.assertEqual(analysis.track_problems(t, stream(), None, damage=(25, "Invalid data"))[0][:2],
                         ("Broken", "decoding errors"))
        self.assertEqual(analysis.track_problems(t, stream(), (fingerprint(), None, None)), [])

    def test_album_of_one_clip_is_likely_broken_but_renamed_rips_are_not(self):
        clip, rip = fingerprint(474), fingerprint()
        album = [track(i, f"Different Song {i}", duration=60.0, folder="/music/Some Artist/Album") for i in range(1, 5)]
        rips = [track(10, "Theme 1", folder="/music/a", album="Game Rip A"), track(11, "Main Theme", folder="/music/b", album="Game Rip B")]
        tracks = album + rips
        fps = {k: (noisy_copy(clip, 0.0), None, None) for k in range(4)}
        fps.update({4: (rip, None, None), 5: (noisy_copy(rip), None, None)})
        streams = {i: stream(-4.5, 0.4, 1.0) for i in range(1, 5)}
        streams.update({10: stream(), 11: stream()})
        pairs = [(a, b, 0.0) for a in range(4) for b in range(a + 1, 4)] + [(4, 5, 0.01)]
        res = run(tracks, fps, streams, pairs=pairs)
        flagged = {res.tracks[b["k"]]["id"]: b["problems"] for b in res.broken}
        self.assertEqual(sorted(i for i, ps in flagged.items() if any("same audio" in p[1] for p in ps)), [1, 2, 3, 4])
        self.assertIn("preview clips", flagged[1][0][2] + str(flagged[1]))
        self.assertNotIn(10, flagged)
        self.assertEqual(res.broken[0]["folder_flagged"], "4 of 4 tracks in this folder")

    def test_broken_copy_is_never_the_keeper(self):
        a = fingerprint()
        tracks = [track(1, "Song", codec="flac", bitrate=1000), track(2, "Song", codec="mp3", bitrate=128)]
        fps = {0: (a, None, None), 1: (noisy_copy(a), None, None)}
        streams = {1: stream(-12, 6, 5000.0), 2: stream(-12)}
        res = run(tracks, fps, streams)
        self.assertEqual(groups(res), [[2, 1]])


if __name__ == "__main__":
    unittest.main()
