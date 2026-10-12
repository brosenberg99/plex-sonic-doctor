"""Keep playlists and collections whole when copies are moved out.

Where a moved copy appears in a playlist (or a track collection), the copy that was kept of the
same recording takes its place, in the same position. If the kept copy is already in that list,
the moved one is just taken out. Smart playlists and smart collections are left alone: Plex fills
those in itself.

Every change is logged per batch (moves/playlists/<batch>.jsonl), so undo can put the original
entries back where they were.
"""
import json
import os
import time
from collections import defaultdict
from typing import Callable, Dict, List, Optional

from .mover import cell

REMOVE_WORDS = {"remove", "r", "x", "delete", "del", "yes", "y", "move"}


def replacements(rows: List[dict], moving_files: set) -> Dict[str, dict]:
    """For each file being moved: the copy kept of the same recording, from its identical-copies
    group. Rows need group, file, decision, plex_id, and kind ("identical"/"versions"; report CSVs
    are recognised by their columns). Returns {moved file: {"old_id", "new_id", "new_title"}}."""
    groups = defaultdict(list)
    for r in rows:
        kind = r.get("kind") or ("versions" if "version" in r else "identical")
        if kind == "identical":
            groups[cell(r, "group")].append(r)
    out = {}
    for rs in groups.values():
        if not any(cell(r, "file") in moving_files for r in rs):
            continue
        kept = [r for r in rs if cell(r, "decision").lower() not in REMOVE_WORDS and cell(r, "file") not in moving_files]
        kept.sort(key=lambda r: cell(r, "suggestion") != "KEEP")     # the suggested keeper first
        keeper = next((r for r in kept if os.path.exists(cell(r, "file"))), None)
        if keeper is None:
            continue
        for r in rs:
            if cell(r, "file") in moving_files and cell(r, "plex_id") and cell(keeper, "plex_id"):
                out[cell(r, "file")] = {"old_id": int(cell(r, "plex_id")), "new_id": int(cell(keeper, "plex_id")),
                                        "new_title": cell(keeper, "title"), "old_title": cell(r, "title")}
    return out


def _containers(server, section_keys: List[str]):
    """Normal (not smart) audio playlists, and track collections in the music libraries."""
    out = []
    for p in server.playlists(playlistType="audio"):
        if not p.smart:
            out.append(("playlist", p))
    for s in server.library.sections():
        if s.type == "artist" and (not section_keys or str(s.key) in section_keys):
            for c in s.collections():
                if not c.smart and c.subtype == "track":
                    out.append(("collection", c))
    return out


def plan(server, section_keys: List[str], repl: Dict[str, dict]) -> List[dict]:
    """Every playlist/collection entry that would change. Reads only."""
    by_old = {v["old_id"]: dict(v, file=f) for f, v in repl.items()}
    edits = []
    if not by_old:
        return edits
    for kind, c in _containers(server, section_keys):
        ids = [getattr(i, "ratingKey", None) for i in c.items()]
        for pos, rk in enumerate(ids):
            if rk in by_old:
                r = by_old[rk]
                edits.append({"kind": kind, "container_id": c.ratingKey, "container": c.title, "position": pos,
                              "after_id": ids[pos - 1] if pos > 0 else None, "old_id": rk, "new_id": r["new_id"],
                              "old_title": r["old_title"], "new_title": r["new_title"], "file": r["file"],
                              "keeper_already_there": r["new_id"] in ids})
    return edits


def _container(server, kind, cid):
    """A fresh copy from the server: plexapi caches a list's items, so after any change the
    list must be fetched again to see it."""
    return server.fetchItem(int(cid))


def _find(items, rating_key):
    return next((i for i in items if getattr(i, "ratingKey", None) == rating_key), None)


def _after(items, after_id, swaps):
    """The entry to insert after: the original neighbour, or what replaced it."""
    if not after_id:
        return None
    return _find(items, after_id) or _find(items, swaps.get(after_id))


def apply(server, edits: List[dict], log_path: str, progress: Callable = lambda m, f=-1: None) -> dict:
    done, failed = 0, []
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    swaps = defaultdict(dict)   # per list: old id -> the id now in its place
    for n, e in enumerate(sorted(edits, key=lambda e: (e["container_id"], e["position"])), start=1):
        try:
            fresh = lambda: _container(server, e["kind"], e["container_id"])
            c = fresh()
            if _find(c.items(), e["old_id"]) is None:
                continue
            added = False
            if not _find(c.items(), e["new_id"]):
                c.addItems([server.fetchItem(e["new_id"])])
                added = True
                try:
                    if e["kind"] == "playlist":
                        c = fresh()
                        items = c.items()
                        c.moveItem(_find(items, e["new_id"]), after=_after(items, e["after_id"], swaps[e["container_id"]]))
                    c = fresh()
                    c.removeItems([_find(c.items(), e["old_id"])])
                except Exception:
                    # Don't leave an extra entry behind: take the kept copy out again.
                    c = fresh()
                    extra = _find(c.items(), e["new_id"])
                    if extra is not None:
                        c.removeItems([extra])
                    raise
            else:
                c.removeItems([_find(c.items(), e["old_id"])])
            swaps[e["container_id"]][e["old_id"]] = e["new_id"]
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(dict(e, added_keeper=added, time=time.strftime("%Y-%m-%d %H:%M:%S")),
                                   ensure_ascii=False) + "\n")
            done += 1
        except Exception as ex:
            failed.append(f"{e['container']}: couldn't replace '{e['old_title']}': {type(ex).__name__}: {ex}")
        progress(f"Updating playlists: {n:,} of {len(edits):,}", n / len(edits))
    return {"updated": done, "failed": failed}


def read_log(log_path: str) -> List[dict]:
    if not os.path.exists(log_path):
        return []
    with open(log_path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def undo(server, log_path: str, restored_files: set, find_by_file: Optional[Callable[[str], Optional[int]]] = None,
         progress: Callable = lambda m, f=-1: None) -> dict:
    """Put playlist entries back for the restored files, in their old positions, and take out the
    kept copy where this tool had added it. If Plex has forgotten the moved track (it rescanned and
    emptied its trash), find_by_file looks up its new id from its path; if Plex hasn't seen the
    file again yet, the entry stays pending (retry after a rescan)."""
    records = read_log(log_path)
    # Top to bottom, so each entry's original neighbour is back before the entry after it.
    todo = sorted([r for r in records if r.get("file") in restored_files and not r.get("undone")],
                  key=lambda r: (r["container_id"], r["position"]))
    swaps = defaultdict(dict)
    for r in records:
        if not r.get("undone"):
            swaps[r["container_id"]][r["old_id"]] = r["new_id"]
    restored, pending, failed = 0, [], []
    for n, r in enumerate(todo, start=1):
        try:
            try:
                track = server.fetchItem(r["old_id"])
            except Exception:
                new_id = find_by_file(r["file"]) if find_by_file else None
                if new_id is None:
                    pending.append(f"{r['container']}: '{r['old_title']}' (Plex hasn't found the file again yet; "
                                   f"rescan, then restore playlists again)")
                    continue
                track = server.fetchItem(new_id)
            fresh = lambda: _container(server, r["kind"], r["container_id"])
            c = fresh()
            if not _find(c.items(), track.ratingKey):
                c.addItems([track])
                if r["kind"] == "playlist":
                    c = fresh()
                    items = c.items()
                    c.moveItem(_find(items, track.ratingKey), after=_after(items, r["after_id"], swaps[r["container_id"]]))
            swaps[r["container_id"]][r["old_id"]] = track.ratingKey   # its id now (new if Plex re-added it)
            if r.get("added_keeper"):
                c = fresh()
                k = _find(c.items(), r["new_id"])
                if k is not None:
                    c.removeItems([k])
            r["undone"] = True
            restored += 1
        except Exception as ex:
            failed.append(f"{r['container']}: couldn't put back '{r['old_title']}': {type(ex).__name__}: {ex}")
        progress(f"Restoring playlists: {n:,} of {len(todo):,}", n / max(len(todo), 1))
    tmp = log_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, log_path)
    return {"restored": restored, "pending": pending, "failed": failed}


def pending_count(log_path: str, held_files: set) -> int:
    """Entries waiting to be put back: their file is back in the library but the entry isn't."""
    return sum(1 for r in read_log(log_path) if not r.get("undone") and r.get("file") not in held_files)
