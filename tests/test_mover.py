"""Move and undo on dummy files in a temp folder: never touches a real library."""
import hashlib
import os
import shutil
import tempfile
import unittest

from plex_sonic_doctor import mover


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def write(p, data):
    with open(p, "wb") as f:
        f.write(data)


def read(p):
    with open(p, "rb") as f:
        return f.read()


class MoverTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="sonicdoctor_test_")
        self.lib, self.hold, self.logs = (os.path.join(self.base, n) for n in ("Music", "holding", "logs"))
        self.files = {}
        for key, rel in {
            "g1_keep": "Artist A/Album/01 Song.flac", "g1_rm": "Artist A/Album MP3/01 Song.mp3",
            "g1_rm2": "01 Song (1).mp3", "g2_a": "B/x.mp3", "g2_b": "B/y.mp3", "g3_rm": "C/old.mp3",
            "g4_maybe": "D/maybe.mp3", "g4_keep": "D/keep.mp3",
            "g5_keep": "Аї - Схема/01 Схема.mp3", "g5_rm": "Аї - Схема (copy)/01 Схема.mp3",
            "g6_keep": "Oblique/デスペラード.mp3", "g6_rm": "Oblique/Disc 2/デスペラード.mp3",
        }.items():
            p = os.path.join(self.lib, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            write(p, os.urandom(500 + len(key)))
            self.files[key] = p
        self.before = {k: sha(p) for k, p in self.files.items()}
        F = self.files
        self.rows = [{"group": g, "decision": d, "file": F[k] if k else os.path.join(self.lib, "C", "gone.flac"),
                      "title": k or "", "artist": "x", "plex_id": "1"} for g, d, k in [
            (1, "", "g1_keep"), (1, "remove", "g1_rm"), (1, "x", "g1_rm2"),
            (2, "remove", "g2_a"), (2, "r", "g2_b"),
            (3, "", None), (3, "remove", "g3_rm"),
            (4, "maybe", "g4_maybe"), (4, "", "g4_keep"),
            (5, "keep", "g5_keep"), (5, "Remove", "g5_rm"),
            (6, "", "g6_keep"), (6, "yes", "g6_rm")]]
        self.should_move = ["g1_rm", "g1_rm2", "g5_rm", "g6_rm"]

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def move(self):
        plan = mover.plan_moves(self.rows, self.hold)
        return plan, mover.execute_moves(plan, self.logs, self.hold)

    def test_plan_applies_the_safety_rules_and_touches_nothing(self):
        plan = mover.plan_moves(self.rows, self.hold)
        self.assertEqual(sorted(r["src"] for r in plan.ready), sorted(self.files[k] for k in self.should_move))
        text = "\n".join(plan.problems)
        self.assertIn("group 2: every copy is marked remove", text)
        self.assertIn("group 3: none of the copies being kept exist", text)
        self.assertIn("decision 'maybe' not understood", text)
        self.assertTrue(all(os.path.exists(p) for p in self.files.values()))
        self.assertFalse(os.path.exists(self.hold))

    def test_move_then_undo_restores_every_byte(self):
        plan, result = self.move()
        self.assertEqual(result["moved"], 4)
        self.assertTrue(all(not os.path.exists(self.files[k]) for k in self.should_move))
        self.assertTrue(all(sha(self.files[k]) == self.before[k] for k in self.files if k not in self.should_move))
        self.assertTrue(os.path.exists(os.path.join(self.hold, plan.batch, "manifest.jsonl")))
        self.assertEqual(mover.batches(self.logs)[0]["held"], 4)
        self.assertEqual(mover.moved_files(self.logs), {self.files[k] for k in self.should_move})
        self.assertEqual(mover.plan_moves(self.rows, self.hold).ready, [])   # running again moves nothing new

        undo = mover.plan_undo(self.logs, plan.batch)
        mover.execute_undo(undo)
        self.assertTrue(all(sha(p) == self.before[k] for k, p in self.files.items()))
        self.assertEqual(mover.moved_files(self.logs), set())
        leftovers = [f for _, _, fs in os.walk(os.path.join(self.hold, plan.batch)) for f in fs if f != "manifest.jsonl"]
        self.assertEqual(leftovers, [])

    def test_undo_never_overwrites_and_can_be_partial(self):
        plan, _ = self.move()
        write(self.files["g1_rm"], b"something new")
        undo = mover.plan_undo(self.logs, plan.batch)
        self.assertEqual(len(undo.ready), 3)
        self.assertIn("already back at the original spot", "\n".join(undo.problems))
        mover.execute_undo(mover.plan_undo(self.logs, plan.batch, match="デスペラード"))
        self.assertEqual(sha(self.files["g6_rm"]), self.before["g6_rm"])
        self.assertFalse(os.path.exists(self.files["g5_rm"]))
        mover.execute_undo(mover.plan_undo(self.logs, plan.batch))
        self.assertEqual(read(self.files["g1_rm"]), b"something new")
        self.assertEqual(sha(self.files["g5_rm"]), self.before["g5_rm"])

    def test_undo_from_the_folder_layout_when_logs_are_lost(self):
        plan, _ = self.move()
        os.remove(os.path.join(self.logs, plan.batch + ".jsonl"))
        os.remove(os.path.join(self.hold, plan.batch, "manifest.jsonl"))
        mover.execute_undo(mover.plan_undo(self.logs, from_folder=os.path.join(self.hold, plan.batch)))
        self.assertTrue(all(sha(p) == self.before[k] for k, p in self.files.items()))

    def test_lost_letters_in_a_resaved_spreadsheet(self):
        rows = [dict(r, file=r["file"].encode("cp1252", "replace").decode("cp1252")) for r in self.rows]
        plan = mover.plan_moves(rows, self.hold)
        self.assertIn("contain '?'", plan.problems[0])
        self.assertEqual(len(plan.ready), 2)

    def test_holding_paths_round_trip(self):
        for original in ["H:\\Music\\A\\b.mp3", "\\\\nas\\share\\A\\b.mp3"]:
            if os.sep == "\\":
                held = mover.holding_path(original, "X:\\hold", "batch1")
                self.assertEqual(mover.original_from_holding(held, "X:\\hold\\batch1"), original)
        if os.sep == "/":
            held = mover.holding_path("/Volumes/m/A/b.mp3", "/tmp/hold", "batch1")
            self.assertEqual(mover.original_from_holding(held, "/tmp/hold/batch1"), "/Volumes/m/A/b.mp3")


if __name__ == "__main__":
    unittest.main()
