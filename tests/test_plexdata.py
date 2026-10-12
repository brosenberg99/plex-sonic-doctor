import os
import sqlite3
import tempfile
import unittest

import numpy as np

from plex_sonic_doctor import plexdata


def make_analysis_folder(root, n_tracks=6, folder="Music Analysis 2", seed=0):
    """A fake Plex 'Music Analysis' folder: Track.tree + Mapping.db in Plex's layout."""
    d = os.path.join(root, "Plug-in Support", "Databases", folder)
    os.makedirs(d)
    rng = np.random.default_rng(seed)
    slots = np.zeros((n_tracks + 2, 53), dtype="<f4")       # two unused slots at the end
    vecs = rng.random((n_tracks, 52)).astype("<f4")
    vecs[1] = vecs[0] * 1.5                                  # same direction, louder: Plex distance 0
    slots[:n_tracks, 1:] = vecs
    slots[:n_tracks, 0] = np.array([1] * n_tracks, dtype="<u4").view("<f4")
    slots.tofile(os.path.join(d, "Track.tree"))
    db = sqlite3.connect(os.path.join(d, "Mapping.db"))
    db.execute("CREATE TABLE mappings ('id' integer, 'external_id' integer, 'metadata_type' integer)")
    db.executemany("insert into mappings values (?,?,?)", [(1000 + i, i, 10) for i in range(n_tracks)])
    db.execute("insert into mappings values (5000, 0, 9)")                 # an album entry: ignored
    db.execute("insert into mappings values (6000, ?, 10)", (n_tracks,))   # points at an unused slot: ignored
    db.commit()
    db.close()
    return d, vecs


class PlexDataTests(unittest.TestCase):
    def test_reads_profiles_and_mapping(self):
        with tempfile.TemporaryDirectory() as root:
            d, vecs = make_analysis_folder(root)
            profiles, slot_of = plexdata.load_profiles(d)
            self.assertEqual(sorted(slot_of), [1000 + i for i in range(6)])
            np.testing.assert_allclose(profiles[slot_of[1003]], vecs[3])

    def test_finds_newest_analysis_folder_from_the_data_folder(self):
        with tempfile.TemporaryDirectory() as root:
            make_analysis_folder(root, folder="Music Analysis 1")
            d2, _ = make_analysis_folder(root, folder="Music Analysis 2")
            self.assertEqual(plexdata.find_music_analysis(root), d2)
            self.assertEqual(plexdata.find_music_analysis(d2), d2)
            self.assertIsNone(plexdata.find_music_analysis(os.path.join(root, "Plug-in Support", "nothing")))

    def test_copy_then_candidate_pairs(self):
        with tempfile.TemporaryDirectory() as root:
            d, _ = make_analysis_folder(root)
            copy = os.path.join(root, "copy")
            plexdata.copy_sonic_files(d, copy, wait=0)
            profiles, slot_of = plexdata.load_profiles(copy)
            ids = sorted(slot_of)
            units = plexdata.unit_vectors(profiles, [slot_of[i] for i in ids])
            i, j, dist = plexdata.candidate_pairs(units, scale=1.08, max_distance=0.06)
            self.assertEqual([(ids[a], ids[b]) for a, b in zip(i, j)], [(1000, 1001)])
            self.assertLess(dist[0], 1e-6)

    def test_fit_scale(self):
        rng = np.random.default_rng(3)
        a = rng.random((40, 52)); b = a + rng.normal(0, 0.05, a.shape)
        ua = a / np.linalg.norm(a, axis=1, keepdims=True); ub = b / np.linalg.norm(b, axis=1, keepdims=True)
        plex_d = 1.11 * np.linalg.norm(ua - ub, axis=1)
        self.assertAlmostEqual(plexdata.fit_scale(ua, ub, plex_d), 1.11, places=6)


if __name__ == "__main__":
    unittest.main()
