"""What the GUI shows and changes: the last scan's results, the user's decisions, and the
filtered, paged views of each report. No HTTP here, so it can be tested directly."""
import json
import os
import threading
import time
from typing import Dict, List, Optional

from .. import config, mover
from ..analysis import NOT_ANALYSED_REASONS, REASON_PROBLEM, SEVERITY_ORDER, fmt_dur

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


class AlbumChecks:
    """The latest recheck of each album folder on the Possibly broken tab, and the albums marked
    as ones that can't be downloaded again. Keyed by folder, saved as they change."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.d: Dict[str, dict] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.d = json.load(f)

    def get(self, folder: str) -> dict:
        return self.d.get(folder) or {}

    def update(self, folder: str, **fields) -> None:
        with self.lock:
            entry = {**self.d.get(folder, {}), **fields}
            self.d[folder] = {k: v for k, v in entry.items() if v not in (None, False)}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.d, fh)
            os.replace(tmp, self.path)


# A folder with more tracks than this is loose files, not an album: a recheck there plays only
# its broken files and new ones, not every song.
RECHECK_WHOLE_FOLDER_UP_TO = 200

# An album's place in the redownload work: never rechecked, every file plays, a fresh copy plays
# but the broken one is still next to it, something still won't play, a broken file is gone and
# nothing replaced it, or marked as not available to download again.
ALBUM_STATUSES = ("todo", "fixed", "replaced", "broken", "missing", "skipped")


def album_status(check: dict, broken_files: List[str]) -> str:
    if check.get("skipped"):
        return "skipped"
    if "files" not in check:
        return "todo"
    bad = [f for f in check["files"] if f["problem"]]
    new_ok = sum(1 for f in check["files"] if f["new"] and not f["problem"])
    broken = {os.path.normcase(f) for f in broken_files}
    gone = sum(1 for f in check.get("gone", []) if os.path.normcase(f) in broken)
    if bad:
        return "replaced" if all(not f["new"] for f in bad) and new_ok >= len(bad) + gone else "broken"
    return "missing" if gone > new_ok else "fixed"


def _match_text(*fields) -> str:
    return " ".join(str(f) for f in fields if f).lower()


class State:
    def __init__(self):
        self.cfg = config.load()
        self.decisions = Decisions(os.path.join(config.data_dir(), "decisions.json"))
        self.album_checks = AlbumChecks(os.path.join(config.data_dir(), "album-checks.json"))
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
        for g in self._live_identical():
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
        for song in self._live_versions():
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
        for b in self._live_broken():
            t = T[b["k"]]
            if severity and b["problems"][0][0] != severity:
                continue
            if problem and not any(problem == p[1] for p in b["problems"]):
                continue
            if q and q not in _match_text(t["title"], t["artist"], t["album"], t["file"]):
                continue
            rows.append(b)
        p = self._page(rows, page, per)
        p["items"] = [dict(self.track(b["k"]), severity=b["problems"][0][0], removable=self.removable(b["k"]),
                           problems=[{"severity": s, "problem": pr, "details": d} for s, pr, d in b["problems"]],
                           folder_flagged=b["folder_flagged"]) for b in p["items"]]
        return p

    # ---------------------------------------------------------------- broken files by album
    # For replacing broken files an album at a time (e.g. downloading iTunes purchases again):
    # each album folder with a broken file, and whether its latest recheck found every file
    # now plays all the way through.

    def _folder_tracks(self) -> Dict[str, List[int]]:
        cached = getattr(self, "_folders", None)
        if cached is None or cached[0] is not self.results:
            folders: Dict[str, List[int]] = {}
            for k, t in enumerate(self.results["tracks"]):
                folders.setdefault(os.path.dirname(t["file"]), []).append(k)
            self._folders = cached = (self.results, folders)
        return cached[1]

    def _fixed_files(self) -> set:
        """Files that played all the way through at their album's latest recheck."""
        return {f["file"] for c in self.album_checks.d.values() for f in c.get("files", []) if not f["problem"]}

    def albums(self, q="", severity="", problem="", status="", sort="artist") -> List[dict]:
        """Album folders with a broken file still in the library (fixed ones too, so they show as
        fixed), filtered and sorted. Each: folder, artist, album, broken (rows), tracks, status."""
        if not self.results:
            return []
        T, by_folder = self.results["tracks"], {}
        for b in self.results["broken"]:
            if self._here(b["k"]):
                by_folder.setdefault(os.path.dirname(T[b["k"]]["file"]), []).append(b)
        folder_tracks = self._folder_tracks()
        out = []
        for folder, rows in by_folder.items():
            ts = [T[b["k"]] for b in rows]
            if severity and not any(b["problems"][0][0] == severity for b in rows):
                continue
            if problem and not any(problem == p[1] for b in rows for p in b["problems"]):
                continue
            artists = {t["artist"] for t in ts}
            a = {"folder": folder, "album": ts[0]["album"], "artist": artists.pop() if len(artists) == 1 else "Various artists",
                 "broken": rows, "tracks": sum(1 for k in folder_tracks.get(folder, []) if self._here(k)),
                 "status": album_status(self.album_checks.get(folder), [t["file"] for t in ts])}
            if q and q not in _match_text(a["album"], a["artist"], folder, *(t["title"] for t in ts)):
                continue
            out.append(a)
        counts = {s: sum(1 for a in out if a["status"] == s) for s in ALBUM_STATUSES}
        if status:
            out = [a for a in out if a["status"] == status]
        text = lambda s: (s or "").casefold()
        keys = {
            "artist": lambda a: (text(a["artist"]), text(a["album"])),
            "album": lambda a: (text(a["album"]), text(a["artist"])),
            "broken": lambda a: (-len(a["broken"]), text(a["artist"]), text(a["album"])),
            "status": lambda a: (ALBUM_STATUSES.index(a["status"]), text(a["artist"]), text(a["album"])),
            "folder": lambda a: text(a["folder"]),
        }
        out.sort(key=keys.get(sort, keys["artist"]))
        self.album_counts = counts
        return out

    def albums_page(self, page=1, per=50, **filters) -> dict:
        p = self._page(self.albums(**filters), page, per)
        p["status_counts"] = self.album_counts if self.results else {s: 0 for s in ALBUM_STATUSES}
        fixed = self._fixed_files()
        items = []
        for a in p["items"]:
            check = self.album_checks.get(a["folder"])
            problems: Dict[str, int] = {}
            for b in a["broken"]:
                problems[b["problems"][0][1]] = problems.get(b["problems"][0][1], 0) + 1
            items.append({
                "folder": a["folder"], "artist": a["artist"], "album": a["album"], "tracks": a["tracks"],
                "status": a["status"], "checked": check.get("checked", ""), "problems": problems,
                "broken": [dict(self.track(b["k"]), severity=b["problems"][0][0], removable=self.removable(b["k"]),
                                fixed=self.results["tracks"][b["k"]]["file"] in fixed,
                                problems=[{"severity": s, "problem": pr, "details": d} for s, pr, d in b["problems"]])
                           for b in a["broken"]],
                # what the recheck found worth showing: files that don't play, new files, files gone
                "checked_files": len(check.get("files", [])),
                "found": [f for f in check.get("files", []) if f["problem"] or f["new"]],
                "gone": check.get("gone", []),
            })
        p["items"] = items
        return p

    def album_folders(self, **filters) -> List[str]:
        return [a["folder"] for a in self.albums(**filters)]

    def recheck_albums(self, folders: List[str], progress=lambda m, f=-1: None, should_stop=lambda: False) -> dict:
        """Play every file in these album folders through, and save what was found. A broken file
        that now plays loses a 'remove' decision, so a fresh download isn't moved by mistake."""
        from .. import filecheck
        known_albums = {a["folder"] for a in self.albums()}
        folders = [f for f in folders if f in known_albums]
        folder_tracks, T = self._folder_tracks(), self.results["tracks"]
        broken_files = {T[b["k"]]["file"] for b in self.results["broken"]}
        statuses: Dict[str, int] = {}
        for n, folder in enumerate(folders):
            if should_stop():
                break
            progress(f"Rechecking albums: {n + 1:,} of {len(folders):,}: {os.path.basename(folder)}", n / max(len(folders), 1))
            known = [T[k]["file"] for k in folder_tracks.get(folder, []) if self._here(k)]
            only = [f for f in known if f in broken_files] if len(known) > RECHECK_WHOLE_FOLDER_UP_TO else None
            found = filecheck.recheck_folder(self.cfg.ffmpeg, folder, known, self.cfg.workers, only)
            self.album_checks.update(folder, checked=time.strftime("%Y-%m-%d %H:%M"), skipped=None, **found)
            now_play = [f["file"] for f in found["files"]
                        if not f["problem"] and f["file"] in broken_files and self.decisions.get(f["file"]) == "remove"]
            self.decisions.set_many(now_play, "")
            st = album_status(self.album_checks.get(folder), [f for f in known if f in broken_files])
            statuses[st] = statuses.get(st, 0) + 1
        progress("Rechecking albums: done", 1)
        return {"rechecked": sum(statuses.values()), "statuses": statuses}

    def skip_album(self, folder: str, skipped: bool) -> None:
        self.album_checks.update(folder, skipped=bool(skipped))

    def mark_old_copies(self, folder: str) -> int:
        """Where a fresh download plays but the broken file is still beside it: mark the broken
        files that still don't play for removal (if they aren't a copy kept elsewhere)."""
        T = self.results["tracks"]
        k_of = {T[k]["file"]: k for k in self._folder_tracks().get(folder, [])}
        files = [f["file"] for f in self.album_checks.get(folder).get("files", []) if f["problem"] and not f["new"]]
        files = [f for f in files if f in k_of and self.removable(k_of[f]) and f not in self.moved
                 and self.decisions.get(f) != "remove"]
        self.decisions.set_many(files, "remove")
        return len(files)

    def check_page(self, page=1, per=50, q="") -> dict:
        if not self.results:
            return self._page([], 1, per)
        T = self.results["tracks"]
        rows = [r for r in self._live_check()
                if not q or q in _match_text(T[r["a"]]["title"], T[r["a"]]["artist"], T[r["a"]]["file"],
                                             T[r["b"]]["title"], T[r["b"]]["artist"], T[r["b"]]["file"])]
        p = self._page(rows, page, per)
        p["items"] = [{"match": r["match"], "sonic": r["sonic"], "a": self.track(r["a"]), "b": self.track(r["b"])}
                      for r in p["items"]]
        return p

    def not_analysed_rows(self, q="", reason="") -> List[dict]:
        if not self.results:
            return []
        T = self.results["tracks"]
        return [r for r in self._live_not_analysed()
                if (not reason or r["reason"] == reason)
                and (not q or q in _match_text(T[r["k"]]["title"], T[r["k"]]["artist"], T[r["k"]]["album"],
                                               T[r["k"]]["file"], r["details"]))]

    def not_analysed_page(self, page=1, per=100, q="", reason="") -> dict:
        p = self._page(self.not_analysed_rows(q, reason), page, per)
        p["items"] = [dict(self.track(r["k"]), reason=r["reason"], details=r["details"],
                           removable=r["reason"] == REASON_PROBLEM and self.removable(r["k"]))
                      for r in p["items"]]
        return p

    # ---------------------------------------------------------------- what's still in the library
    # The lists and counts leave out what's in the holding folder, so they change with every move
    # and undo instead of waiting for the next scan. A group or song down to one copy is done.

    def _here(self, k: int) -> bool:
        return self.results["tracks"][k]["file"] not in self.moved

    def _live_identical(self) -> List[dict]:
        return [g for g in self.results["identical"] if sum(self._here(c["k"]) for c in g["copies"]) > 1]

    def _live_versions(self) -> List[dict]:
        return [s for s in self.results["versions"] if sum(self._here(v["k"]) for v in s["versions"]) > 1]

    def _live_broken(self) -> List[dict]:
        """Also leaves out files that played all the way through at a recheck (replaced since the scan)."""
        T, fixed = self.results["tracks"], self._fixed_files()
        return [b for b in self.results["broken"] if self._here(b["k"]) and T[b["k"]]["file"] not in fixed]

    def _live_check(self) -> List[dict]:
        return [r for r in self.results["check_by_ear"] if self._here(r["a"]) and self._here(r["b"])]

    def _live_not_analysed(self) -> List[dict]:
        rows = self.results.get("not_analysed", [])
        # results saved before reasons were recorded hold bare track numbers
        rows = [r if isinstance(r, dict) else {"k": r, "reason": "", "details": "run a full scan to see why"}
                for r in rows]
        return [r for r in rows if self._here(r["k"])]

    def live_stats(self) -> Optional[dict]:
        """The last scan's numbers, recounted without the files in the holding folder."""
        if not self.results:
            return None
        T, s = self.results["tracks"], dict(self.results["stats"])
        groups = self._live_identical()
        extra, extra_bytes = 0, 0
        for g in groups:
            if g["status"].startswith("Sonic"):
                continue
            here = [T[c["k"]] for c in g["copies"] if self._here(c["k"])]
            extra += len(here) - 1
            extra_bytes += sum(t["size"] for t in here[1:])
        sonic = [g for g in groups if g["status"].startswith("Sonic")]
        songs = self._live_versions()
        broken = self._live_broken()
        unanalysed = self._live_not_analysed()
        s.update({
            "tracks": sum(1 for k in range(len(T)) if self._here(k)),
            "not_analysed": len(unanalysed),
            "not_analysed_reasons": {r: sum(1 for x in unanalysed if x["reason"] == r) for r in NOT_ANALYSED_REASONS},
            "identical_groups": len(groups) - len(sonic), "sonic_only_groups": len(sonic),
            "extra_copies": extra, "extra_bytes": extra_bytes,
            "version_songs": len(songs),
            "versions": sum(1 for x in songs for v in x["versions"] if self._here(v["k"])),
            "broken_files": len(broken),
            "broken_fixed": len(self.results["broken"]) - len(broken) - sum(1 for b in self.results["broken"] if not self._here(b["k"])),
            "broken_by_severity": {sev: sum(1 for b in broken if b["problems"][0][0] == sev) for sev in SEVERITY_ORDER},
            "check_pairs": len(self._live_check()),
        })
        return s

    # ---------------------------------------------------------------- problem files
    # A file that's broken or won't play can be moved on its own (it isn't a copy of anything
    # kept), unless it's in an identical group or a song's versions: then those rules apply.

    def removable(self, k: int) -> bool:
        cached = getattr(self, "_grouped", None)
        if cached is None or cached[0] is not self.results:
            ks = {c["k"] for g in self.results["identical"] for c in g["copies"]}
            ks |= {v["k"] for s in self.results["versions"] for v in s["versions"]}
            self._grouped = cached = (self.results, ks)
        return k not in cached[1]

    def _problem_ks(self) -> List[int]:
        ks = [b["k"] for b in self._live_broken()]
        ks += [r["k"] for r in self._live_not_analysed() if r["reason"] == REASON_PROBLEM]
        return sorted({k for k in ks if self.removable(k)})

    def mark_problem_files(self, q="", reason="") -> int:
        """In the Not analysed list as filtered: mark every problem file 'remove' (if undecided)."""
        T = self.results["tracks"]
        files = [T[r["k"]]["file"] for r in self.not_analysed_rows(q, reason)
                 if r["reason"] == REASON_PROBLEM and self.removable(r["k"])]
        files = [f for f in files if not self.decisions.get(f) and f not in self.moved]
        self.decisions.set_many(files, "remove")
        return len(files)

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
        for k in self._problem_ks():
            rows.append(row(f"{label(k)} (problem file)", k, mover.PROBLEM_KIND))
        return [r for r in rows if r["file"] not in self.moved]

    def plan(self) -> mover.Plan:
        return mover.plan_moves(self.move_rows(), self.cfg.holding_dir)
