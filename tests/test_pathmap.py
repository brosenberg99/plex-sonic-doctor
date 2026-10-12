import unittest

from plex_sonic_doctor.config import PathMap
from plex_sonic_doctor.pathmap import PathMapper, share_found, suggest_map


class PathMapTests(unittest.TestCase):
    def test_windows_drive_swap(self):
        m = PathMapper([PathMap(plex="E:\\", local="H:\\")])
        self.assertEqual(m.to_local("E:\\Music\\A\\b.mp3"), "H:\\Music\\A\\b.mp3")
        self.assertEqual(m.to_local("e:\\Music\\b.mp3"), "H:\\Music\\b.mp3")   # Windows paths ignore case

    def test_linux_server_to_windows_share(self):
        m = PathMapper([PathMap(plex="/data/music", local="\\\\nas\\music")])
        self.assertEqual(m.to_local("/data/music/A/b.flac"), "\\\\nas\\music\\A\\b.flac")

    def test_windows_server_to_mac(self):
        m = PathMapper([PathMap(plex="E:\\Music", local="/Volumes/Music")])
        self.assertEqual(m.to_local("E:\\Music\\A\\b.mp3"), "/Volumes/Music/A/b.mp3")

    def test_longest_prefix_wins_and_partial_names_dont_match(self):
        m = PathMapper([PathMap("/data", "/mnt/a"), PathMap("/data/music", "/mnt/b")])
        self.assertEqual(m.to_local("/data/music/x.mp3"), "/mnt/b/x.mp3")
        self.assertEqual(m.to_local("/data/musicals/x.mp3"), "/mnt/a/musicals/x.mp3")
        self.assertEqual(m.to_local("/other/x.mp3"), "/other/x.mp3")

    def test_suggest_map_from_one_real_file(self):
        exists = {"H:\\Music\\A\\b.mp3"}.__contains__
        self.assertEqual(suggest_map("E:\\Music\\A\\b.mp3", "H:\\", exists), PathMap("E:\\", "H:\\"))
        self.assertEqual(suggest_map("E:\\Music\\A\\b.mp3", "H:\\Music", exists), PathMap("E:\\Music\\", "H:\\Music"))
        exists = {"/Volumes/m/A/b.mp3"}.__contains__
        self.assertEqual(suggest_map("/data/music/A/b.mp3", "/Volumes/m", exists), PathMap("/data/music/", "/Volumes/m"))
        self.assertIsNone(suggest_map("/data/music/A/b.mp3", "/elsewhere", exists))

    def test_share_found(self):
        m = PathMapper([PathMap("E:\\", "H:\\")])
        exists = {"H:\\a.mp3"}.__contains__
        self.assertEqual(share_found(m, ["E:\\a.mp3", "E:\\b.mp3"], exists), 0.5)


if __name__ == "__main__":
    unittest.main()
