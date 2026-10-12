"""What the GUI shows and changes: the last scan's results, the user's decisions, and the
filtered, paged views of each report. No HTTP here, so it can be tested directly."""
import json
import os
import threading
from typing import Dict, List, Optional

from .. import config, mover
from ..analysis import fmt_dur

DECISIONS = ("remove", "keep")


class Decisions:
    """One decision per file (keyed by its path), saved as they're made. A file that's both a
    duplicate group's keeper and a version shows the same decision in both places."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.d: Dict[str, str] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.d = json.load(f)

    def get(self, file: str) -> str:
        return self.d.get(file, "")

    def set_many(self, files: List[str], decision: str) -> None:
        with self.lock:
            for f in files:
                if decision in DECISIONS:
                    self.d[f] = decision
                else:
                    self.d.pop(f, None)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.d, fh)
            os.replace(tmp, self.path)


def _match_text(*fields) -> str:
    return " ".join(str(f) for f in fields if f).lower()


class State:
    def __init__(self):
        self.cfg = config.load()
        self.decisions = Decisions(os.path.join(config.data_dir(), "decisions.json"))
        self.results: Optional[dict] = None
        self.moved: set = set()
        self.reload()

    # ---------------------------------------------------------------- loading

    def reload(self) -> None:
        self.cfg = config.load()
        path = os.path.join(config.sub_dir("reports"), "results.json")
        self.results = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.results = json.load(f)
        self.refresh_moved()

    def refresh_moved(self) -> None:
        self.moved = mover.moved_files(config.sub_dir("moves"))

    def track(self, k: int) -> dict:
        t = self.results["tracks"][k]
        return {"k": k, "id": t["id"], "title": t["title"], "artist": t["artist"], "album": t["album"],
                "length": fmt_dur(t["duration"]), "duration": t["duration"], "codec": t["codec"],
                "bitrate": t["bitrate"], "size": t["size"], "file": t["file"],
                "loudness": t.get("loudness"), "silent": t.get("silent"), "peak": t.get("peak"),
                "channels": {1: "mono", 2: "stereo"}.get(t.get("channels"), "" if not t.get("channels") else f"{t['channels']:.0f} ch"),
                "decision": self.decisions.get(t["file"]), "moved": t["file"] in self.moved}

    # ---------------------------------------------------------------- views

    def _page(self, items: list, page: int, per: int) -> dict:
        page = max(1, page)
        return {"total": len(items), "page": page, "per": per, "pages": max(1, -(-len(items) // per)),
                "items": items[(page - 1) * per: page * per]}

    def identical_groups(self, q="", status="", note="", marked="", fmt="") -> List[dict]:
        if not self.results:
            return []
        T, out = self.results["tracks"], []
        for g in self.results["identical"]:
            ks = [c["k"] for c in g["copies"]]
            if status == "identical" and g["status"].startswith("Sonic"):
                continue
            if status == "sonic" and not g["status"].startswith("Sonic"):
                continue
            if note == "titles" and "titles differ" not in g["note"]:
                continue
            if note == "none" and g["note"]:
                continue
            if note == "versions" and not g["other_versions"]:
                continue
            if fmt and not any(T[k]["codec"] == fmt for k in ks):
                continue
            if marked:
                ds = [self.decisions.get(T[k]["file"]) for k in ks]
                if marked == "undecided" and any(ds):
                    continue
                if marked == "decided" and not any(ds):
                    continue
            if q and q not in _match_text(*(f"{T[k]['title']} {T[k]['artist']} {T[k]['album']} {T[k]['file']}" for k in ks)):
                continue
            out.append(g)
        return out

    def identical_page(self, page=1, per=40, **filters) -> dict:
        groups = self.identical_groups(**filters)
        p = self._page(groups, page, per)
        p["items"] = [{"group": g["group"], "status": g["status"], "note": g["note"], "other_versions": g["other_versions"],
                       "copies": [dict(self.track(c["k"]), match=c["match"], sonic_only=c["sonic_only"],
                                       suggestion="KEEP" if n == 0 else "remove?")
                                  for n, c in enumerate(g["copies"])]}
                      for g in p["items"]]
        return p

    def version_songs(self, q="", reason="", marked="") -> List[dict]:
        if not self.results:
            return []
        T, out = self.results["tracks"], []
        for song in self.results["versions"]:
            ks = [v["k"] for v in song["versions"]]
            if reason and not any(reason in r for v in song["versions"] for r in v["reasons"]):
                continue
            if marked:
                ds = [self.decisions.get(T[k]["file"]) for k in ks]
                if marked == "undecided" and any(ds):
                    continue
                if marked == "decided" and not any(ds):
                    continue
            if q and q not in _match_text(*(f"{T[k]['title']} {T[k]['artist']} {T[k]['album']} {T[k]['file']}" for k in ks)):
                continue
            out.append(song)
        return out

    def versions_page(self, page=1, per=40, **filters) -> dict:
        p = self._page(self.version_songs(**filters), page, per)
        p["items"] = [{"group": s["group"], "versions": [dict(self.track(v["k"]), number=n, reasons=v["reasons"],
                                                              match=v["match"], identical_copies=v["identical_copies"],
                                                              identical_group=v["identical_group"])
                                                         for n, v in enumerate(s["versions"], start=1)]}
                      for s in p["items"]]
        return p

    def broken_page(self, page=1, per=100, q="", severity="", problem="") -> dict:
        if not self.results:
            return self._page([], 1, per)
        T, rows = self.results["tracks"], []
        for b in self.results["broken"]:
            t = T[b["k"]]
            if severity and b["problems"][0][0] != severity:
                continue
            if problem and not any(problem == p[1] for p in b["problems"]):
                continue
            if q and q not in _match_text(t["title"], t["artist"], t["album"], t["file"]):
                continue
            rows.append(b)
        p = self._page(rows, page, per)
        p["items"] = [dict(self.track(b["k"]), severity=b["problems"][0][0],
                           problems=[{"severity": s, "problem": pr, "details": d} for s, pr, d in b["problems"]],
                           folder_flagged=b["folder_flagged"]) for b in p["items"]]
        return p

    def check_page(self, page=1, per=50, q="") -> dict:
        if not self.results:
            return self._page([], 1, per)
        T = self.results["tracks"]
        rows = [r for r in self.results["check_by_ear"]
                if not q or q in _match_text(T[r["a"]]["title"], T[r["a"]]["artist"], T[r["a"]]["file"],
                                             T[r["b"]]["title"], T[r["b"]]["artist"], T[r["b"]]["file"])]
        p = self._page(rows, page, per)
        p["items"] = [{"match": r["match"], "sonic": r["sonic"], "a": self.track(r["a"]), "b": self.track(r["b"])}
                      for r in p["items"]]
        return p

    # ---------------------------------------------------------------- decisions in bulk

    def mark_suggested(self, **filters) -> int:
        """In every identical group the filters show: mark the non-keepers 'remove' (if undecided)."""
        T, files = self.results["tracks"], []
        for g in self.identical_groups(**filters):
            for c in g["copies"][1:]:
                f = T[c["k"]]["file"]
                if not self.decisions.get(f) and f not in self.moved:
                    files.append(f)
        self.decisions.set_many(files, "remove")
        return len(files)

    def clear_filtered(self, tab: str, **filters) -> int:
        T = self.results["tracks"]
        if tab == "identical":
            files = [T[c["k"]]["file"] for g in self.identical_groups(**filters) for c in g["copies"]]
        else:
            files = [T[v["k"]]["file"] for s in self.version_songs(**filters) for v in s["versions"]]
        files = [f for f in files if self.decisions.get(f)]
        self.decisions.set_many(files, "")
        return len(files)

    def marked_summary(self) -> dict:
        if not self.results:
            return {"files": 0, "bytes": 0}
        size = {t["file"]: t["size"] for t in self.results["tracks"]}
        files = [f for f, d in self.decisions.d.items() if d == "remove" and f in size and f not in self.moved]
        return {"files": len(files), "bytes": sum(size[f] for f in files)}

    # ---------------------------------------------------------------- moving

    def move_rows(self) -> List[dict]:
        """Rows for mover.plan_moves: every identical group and every song's versions, with the
        current decisions. The mover's rules then keep at least one copy in each."""
        if not self.results:
            return []
        T, rows = self.results["tracks"], []

        def row(group, k, kind, suggestion=""):
            t = T[k]
            return {"group": group, "file": t["file"], "decision": self.decisions.get(t["file"]),
                    "title": t["title"], "artist": t["artist"], "plex_id": t["id"], "kind": kind,
                    "suggestion": suggestion}

        # The group label is what the mover shows when it skips one, so it names the song.
        label = lambda k: f"“{T[k]['title']}” by {T[k]['artist'] or 'unknown artist'}"
        for g in self.results["identical"]:
            name = f"{label(g['copies'][0]['k'])} (identical copies, group {g['group']})"
            rows += [row(name, c["k"], "identical", "KEEP" if n == 0 else "remove?") for n, c in enumerate(g["copies"])]
        for s in self.results["versions"]:
            name = f"{label(s['versions'][0]['k'])} (versions, {s['group']})"
            rows += [row(name, v["k"], "versions") for v in s["versions"]]
        return [r for r in rows if r["file"] not in self.moved]

    def plan(self) -> mover.Plan:
        return mover.plan_moves(self.move_rows(), self.cfg.holding_dir)
