"""Playlist/collection replacement and undo, against an in-memory stand-in for a Plex server."""
import os
import shutil
import tempfile
import unittest

from plex_sonic_doctor import plexlists


class Item:
    def __init__(self, rk):
        self.ratingKey = rk


class FakeList:
    """Like plexapi: items() is read once per fetched object and then cached, so a change only
    shows up after the list is fetched from the server again."""
    def __init__(self, rk, title, ids, smart=False, subtype="track"):
        self.ratingKey, self.title, self.ids, self.smart, self.subtype = rk, title, list(ids), smart, subtype
        self._cache = None

    def fetched(self):
        view = FakeList.__new__(FakeList)
        view.__dict__.update(self.__dict__)
        view._cache = None
        view.ids = self.ids          # same underlying list: changes reach the "server"
        return view

    def items(self):
        if self._cache is None:
            self._cache = [Item(i) for i in self.ids]
        return self._cache

    def addItems(self, items):
        self.ids += [i.ratingKey for i in items]

    def removeItems(self, items):
        for i in items:
            self.ids.remove(i.ratingKey)

    def moveItem(self, item, after=None):
        self.ids.remove(item.ratingKey)
        self.ids.insert(0 if after is None else self.ids.index(after.ratingKey) + 1, item.ratingKey)


class FakeSection:
    type, key = "artist", "2"

    def __init__(self, cols):
        self.cols = cols

    def collections(self):
        return self.cols


class FakeServer:
    def __init__(self, playlists, collections, tracks):
        self.pls, self.tracks = playlists, set(tracks)
        self.library = type("L", (), {"sections": lambda _self: [FakeSection(collections)]})()
        self.by_id = {p.ratingKey: p for p in playlists + collections}

    def playlists(self, playlistType=None):
        return self.pls

    def fetchItem(self, rk):
        if rk in self.by_id:
            return self.by_id[rk].fetched()
        if rk in self.tracks:
            return Item(rk)
        raise LookupError(rk)


class PlaylistTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sonicdoctor_pl_")
        self.log = os.path.join(self.dir, "playlists", "batch.jsonl")
        # Tracks: 1/11 are copies of song A (1 kept), 2/12 of B (2 kept), 3/13 of C (3 kept).
        self.files = {}
        for rk in (1, 2, 3, 11, 12, 13):
            p = os.path.join(self.dir, f"{rk}.mp3")
            open(p, "wb").close()
            self.files[rk] = p

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def rows(self):
        f = self.files
        return [{"group": g, "file": f[k], "decision": d, "plex_id": k, "title": t, "kind": "identical",
                 "suggestion": s} for g, k, d, t, s in [
            ("A", 1, "", "A", "KEEP"), ("A", 11, "remove", "A copy", "remove?"),
            ("B", 2, "", "B", "KEEP"), ("B", 12, "remove", "B copy", "remove?"),
            ("C", 3, "", "C", "KEEP"), ("C", 13, "remove", "C copy", "remove?")]] + [
            {"group": "V1", "file": f[13], "decision": "remove", "plex_id": 13, "title": "C copy", "kind": "versions"}]

    def server(self):
        self.pl = FakeList(100, "Mix", [11, 50, 12, 13, 60])       # moved copies at the top, and side by side
        self.dupe = FakeList(101, "Has both", [70, 1, 11, 80])        # the kept copy is already there
        self.smart = FakeList(102, "Smart", [11], smart=True)
        self.col = FakeList(200, "Track collection", [12, 90])
        self.album_col = FakeList(201, "Albums", [12], subtype="album")
        return FakeServer([self.pl, self.dupe, self.smart], [self.col, self.album_col], [1, 2, 3, 50, 60, 70, 80, 90])

    def test_replacement_keeps_positions_and_undo_restores_them(self):
        server = self.server()
        moving = {self.files[k] for k in (11, 12, 13)}
        repl = plexlists.replacements(self.rows(), moving)
        self.assertEqual({v["old_id"]: v["new_id"] for v in repl.values()}, {11: 1, 12: 2, 13: 3})
        edits = plexlists.plan(server, [], repl)
        self.assertEqual(sorted((e["container"], e["old_id"]) for e in edits),
                         [("Has both", 11), ("Mix", 11), ("Mix", 12), ("Mix", 13), ("Track collection", 12)])
        result = plexlists.apply(server, edits, self.log)
        self.assertEqual(result, {"updated": 5, "failed": []})
        self.assertEqual(self.pl.ids, [1, 50, 2, 3, 60])
        self.assertEqual(self.dupe.ids, [70, 1, 80])             # no second copy of the kept track
        self.assertEqual(self.smart.ids, [11])                    # smart playlists untouched
        self.assertEqual(sorted(self.col.ids), [2, 90])
        self.assertEqual(self.album_col.ids, [12])                # album collections untouched

        server.tracks |= {11, 12, 13}                             # files back, Plex still knows the old ids
        undo = plexlists.undo(server, self.log, moving)
        self.assertEqual(undo["restored"], 5)
        self.assertEqual(self.pl.ids, [11, 50, 12, 13, 60])
        self.assertEqual(self.dupe.ids, [70, 1, 11, 80])
        self.assertEqual(sorted(self.col.ids), [12, 90])
        self.assertEqual(plexlists.undo(server, self.log, moving)["restored"], 0)   # nothing done twice

    def test_undo_after_plex_forgot_the_tracks(self):
        server = self.server()
        moving = {self.files[11]}
        plexlists.apply(server, plexlists.plan(server, [], plexlists.replacements(self.rows(), moving)), self.log)
        # Plex emptied its trash: id 11 is gone. Before a rescan it can't be found at all...
        r = plexlists.undo(server, self.log, moving, find_by_file=lambda p: None)
        self.assertEqual((r["restored"], len(r["pending"])), (0, 2))
        self.assertEqual(plexlists.pending_count(self.log, set()), 2)
        # ...after a rescan it's back under a new id, found by its path.
        server.tracks.add(511)
        r = plexlists.undo(server, self.log, moving, find_by_file=lambda p: 511 if p == self.files[11] else None)
        self.assertEqual(r["restored"], 2)
        self.assertEqual(self.pl.ids, [511, 50, 12, 13, 60])
        self.assertEqual(self.dupe.ids, [70, 1, 511, 80])

    def test_versions_and_unkept_groups_get_no_replacement(self):
        rows = self.rows()
        for r in rows:
            if r["group"] == "A":
                r["decision"] = "remove"
        repl = plexlists.replacements(rows, {self.files[1], self.files[11]})
        self.assertEqual(repl, {})


if __name__ == "__main__":
    unittest.main()
