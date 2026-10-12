"""Move the copies a user marked into a holding folder, and put them back (undo).

Each file goes to <holding>/<batch>/<drive tag>/<original path>, so the holding folder mirrors
where every file came from. Every move and restore is logged as it happens, to the app's
moves/<batch>.jsonl and to a manifest.jsonl inside the holding batch folder; if both were lost,
plan_undo(from_folder=...) rebuilds the original locations from the folder layout.

Safety rules: nothing happens until execute() is called; nothing is ever overwritten in either
direction; a group always keeps at least one copy that exists on disk.
"""
import csv
import json
import os
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

HOLDING_DIR_NAME = "_duplicates_holding"
REMOVE_WORDS = {"remove", "r", "x", "delete", "del", "yes", "y", "move"}
KEEP_WORDS = {"", "keep", "k", "no", "n"}


# ---------------------------------------------------------------- decisions

def read_decisions(path: str) -> tuple:
    """Rows from an edited CSV (or .xlsx if openpyxl is installed). Returns (rows, warning)."""
    if path.lower().endswith(".xlsx"):
        try:
            import openpyxl
        except ImportError:
            raise RuntimeError("Reading .xlsx needs the openpyxl package; save the sheet as 'CSV UTF-8' instead.")
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).worksheets[0]
        it = ws.iter_rows(values_only=True)
        header = [str(h or "").strip() for h in next(it)]
        return [dict(zip(header, ["" if v is None else v for v in row])) for row in it], ""
    with open(path, "rb") as f:
        raw = f.read()
    try:
        return list(csv.DictReader(raw.decode("utf-8-sig").splitlines())), ""
    except UnicodeDecodeError:
        return (list(csv.DictReader(raw.decode("cp1252", errors="replace").splitlines())),
                "The file isn't UTF-8, so non-English file names may not be found; save it as 'CSV UTF-8' in Excel.")


def cell(row: dict, name: str) -> str:
    v = row.get(name, "")
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


# ---------------------------------------------------------------- paths and logs

def _mount_point(path: str) -> str:
    path = os.path.abspath(path)
    while not os.path.ismount(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def holding_root_for(path: str, override: str = "") -> str:
    """The holding folder for a file: the user's choice, or one at the top of the file's own
    drive/volume (so a move is a quick rename, not a copy)."""
    if override:
        return override
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if drive and not drive.startswith("\\\\"):
        return drive + os.sep + HOLDING_DIR_NAME
    return os.path.join(_mount_point(path), HOLDING_DIR_NAME)


def holding_path(original: str, root: str, batch: str) -> str:
    drive, rest = os.path.splitdrive(os.path.abspath(original))
    if drive.startswith("\\\\"):                      # \\server\share\... -> UNC\server\share\...
        tag, rest = "UNC", drive.lstrip("\\") + rest
    elif drive:                                       # H:\... -> H\...
        tag = drive.rstrip(":")
    else:                                             # /Volumes/music/... -> root/Volumes/music/...
        tag = "root"
    return os.path.join(root, batch, tag, rest.lstrip("\\/"))


def original_from_holding(held: str, batch_dir: str) -> str:
    """Inverse of holding_path, for rebuilding a lost log from the folder layout."""
    rel = os.path.relpath(held, batch_dir)
    tag, _, rest = rel.replace("\\", "/").partition("/")
    if tag == "UNC":
        return "\\\\" + rest.replace("/", "\\")
    if tag == "root":
        return "/" + rest
    return tag + ":\\" + rest.replace("/", "\\")


def append_log(paths, record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False) + "\n"
    for p in paths:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def read_log(path: str) -> List[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def move_file(src: str, dst: str) -> int:
    """Rename when possible (same drive); otherwise copy, check the size, then delete the source."""
    size = os.path.getsize(src)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError:
        shutil.copy2(src, dst)
        if os.path.getsize(dst) != size:
            os.remove(dst)
            raise OSError("copy came out the wrong size; original left in place")
        os.remove(src)
    if os.path.getsize(dst) != size or os.path.exists(src):
        raise OSError("move could not be verified")
    return size


def fmt_size(n: int) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


# ---------------------------------------------------------------- move

@dataclass
class Plan:
    batch: str
    ready: List[dict] = field(default_factory=list)      # {"row", "src", "dst", "size"}
    problems: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(r["size"] for r in self.ready)


def plan_moves(rows: List[dict], holding_override: str = "") -> Plan:
    """Rows need 'group', 'file' and 'decision'. Applies the safety rules; touches nothing."""
    plan = Plan(batch=time.strftime("%Y-%m-%d_%H%M%S"))
    groups = defaultdict(list)
    for r in rows:
        if cell(r, "file"):
            groups[cell(r, "group")].append(r)
    garbled = sum(1 for r in rows if "?" in cell(r, "file"))
    if garbled:
        # Windows file names can't contain '?': Excel puts it in place of letters an older
        # CSV format can't hold, so those rows can't be matched to files.
        plan.problems.append(f"{garbled} file names contain '?', so the sheet lost non-English letters when it was "
                             f"saved. Those groups are skipped. Save it as 'CSV UTF-8' in Excel to include them.")
    candidates = []
    for g, rs in groups.items():
        decisions = [cell(r, "decision").lower() for r in rs]
        for r, d in zip(rs, decisions):
            if d not in REMOVE_WORDS and d not in KEEP_WORDS:
                plan.problems.append(f"group {g}: decision '{cell(r, 'decision')}' not understood, left alone: {cell(r, 'file')}")
        marked = [r for r, d in zip(rs, decisions) if d in REMOVE_WORDS]
        if not marked:
            continue
        staying = [r for r, d in zip(rs, decisions) if d not in REMOVE_WORDS]
        if not staying:
            plan.problems.append(f"group {g}: every copy is marked remove; skipped (one copy must stay)")
            continue
        candidates.append((g, marked, staying))

    # Look at every file involved once, in parallel: over a network share, one-at-a-time checks
    # of thousands of files take minutes.
    sizes = _sizes({cell(r, "file") for _, m, s in candidates for r in m + s})
    marked_rows = []
    for g, marked, staying in candidates:
        if not any(sizes.get(cell(r, "file")) is not None for r in staying):
            plan.problems.append(f"group {g}: none of the copies being kept exist on disk; skipped")
            continue
        marked_rows.extend(marked)
    roots = {holding_root_for(cell(r, "file"), holding_override) for r in marked_rows}
    batch_dir_exists = any(os.path.exists(os.path.join(root, plan.batch)) for root in roots)
    seen = set()
    for r in marked_rows:
        src = cell(r, "file")
        if src.lower() in seen:     # the same file can appear in two groups (a version and its copies)
            continue
        seen.add(src.lower())
        dst = holding_path(src, holding_root_for(src, holding_override), plan.batch)
        if sizes.get(src) is None:
            plan.problems.append(f"not found (already moved?), skipped: {src}")
        elif batch_dir_exists and os.path.exists(dst):   # a new batch's folder can't hold anything yet
            plan.problems.append(f"something is already at the holding spot, skipped: {dst}")
        else:
            plan.ready.append({"row": r, "src": src, "dst": dst, "size": sizes[src]})
    return plan


def _sizes(files, workers: int = 16) -> Dict[str, Optional[int]]:
    """{file: size, or None if it isn't there}, checked in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    def size(f):
        try:
            return f, os.stat(f).st_size
        except OSError:
            return f, None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return dict(pool.map(size, files))


def execute_moves(plan: Plan, logs_dir: str, holding_override: str = "",
                  progress: Callable = lambda m, f=-1: None) -> dict:
    local_log = os.path.join(logs_dir, plan.batch + ".jsonl")
    moved, failed = 0, []
    for n, item in enumerate(plan.ready, start=1):
        src, dst, r = item["src"], item["dst"], item["row"]
        root = holding_root_for(src, holding_override)
        try:
            size = move_file(src, dst)
        except OSError as e:
            failed.append(f"couldn't move {src}: {e}")
            continue
        append_log([local_log, os.path.join(root, plan.batch, "manifest.jsonl")],
                   {"action": "moved", "batch": plan.batch, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "original": src, "held": dst, "size": size, "group": cell(r, "group"),
                    "title": cell(r, "title"), "artist": cell(r, "artist"), "plex_id": cell(r, "plex_id")})
        moved += 1
        progress(f"Moved {n:,} of {len(plan.ready):,}", n / len(plan.ready))
    return {"batch": plan.batch, "moved": moved, "failed": failed}


# ---------------------------------------------------------------- batches and undo

def _state(records: List[dict]) -> Dict[str, dict]:
    state = {}
    for rec in records:
        state[rec["original"]] = rec
    return state


def batches(logs_dir: str) -> List[dict]:
    out = []
    if not os.path.isdir(logs_dir):
        return out
    for name in sorted(os.listdir(logs_dir)):
        if name.endswith(".jsonl"):
            state = _state(read_log(os.path.join(logs_dir, name)))
            held = [r for r in state.values() if r["action"] == "moved"]
            out.append({"batch": name[:-6], "held": len(held), "held_bytes": sum(r["size"] for r in held),
                        "restored": sum(1 for r in state.values() if r["action"] == "restored")})
    return out


def moved_files(logs_dir: str) -> set:
    """Original paths of files currently in the holding folder."""
    held = set()
    for b in batches(logs_dir):
        for rec in _state(read_log(os.path.join(logs_dir, b["batch"] + ".jsonl"))).values():
            if rec["action"] == "moved":
                held.add(rec["original"])
    return held


@dataclass
class UndoPlan:
    batch: str
    logs: List[str]
    ready: List[dict] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)


def plan_undo(logs_dir: str, batch: str = "", from_folder: str = "", match: str = "") -> UndoPlan:
    if from_folder:
        batch_dir = os.path.abspath(from_folder)
        batch = os.path.basename(batch_dir)
        records = []
        for dirpath, _, files in os.walk(batch_dir):
            for fn in files:
                held = os.path.join(dirpath, fn)
                if held != os.path.join(batch_dir, "manifest.jsonl"):
                    records.append({"action": "moved", "batch": batch, "original": original_from_holding(held, batch_dir),
                                    "held": held, "size": os.path.getsize(held)})
        logs = [os.path.join(logs_dir, batch + ".jsonl"), os.path.join(batch_dir, "manifest.jsonl")]
    else:
        local_log = os.path.join(logs_dir, batch + ".jsonl")
        if not os.path.exists(local_log):
            raise FileNotFoundError(f"No log for batch {batch}. Use the list of batches, or undo from the holding folder.")
        records, logs = read_log(local_log), [local_log]
    plan = UndoPlan(batch=batch, logs=logs)
    todo = [r for r in _state(records).values() if r["action"] == "moved"]
    if match:
        todo = [r for r in todo if match.lower() in r["original"].lower()]
    for r in todo:
        if not os.path.exists(r["held"]):
            plan.problems.append(f"not in the holding folder any more, skipped: {r['held']}")
        elif os.path.exists(r["original"]):
            plan.problems.append(f"a file is already back at the original spot, skipped (nothing overwritten): {r['original']}")
        else:
            plan.ready.append(r)
    return plan


def execute_undo(plan: UndoPlan, progress: Callable = lambda m, f=-1: None) -> dict:
    restored, failed, holding_dirs = 0, [], set()
    marker = os.sep + plan.batch + os.sep
    for n, r in enumerate(plan.ready, start=1):
        cut = r["held"].find(marker)
        batch_dir = r["held"][: cut + len(marker) - 1] if cut >= 0 else os.path.dirname(r["held"])
        holding_dirs.add(batch_dir)
        try:
            move_file(r["held"], r["original"])
        except OSError as e:
            failed.append(f"couldn't put back {r['original']}: {e}")
            continue
        append_log(set(plan.logs) | {os.path.join(batch_dir, "manifest.jsonl")},
                   {**r, "action": "restored", "time": time.strftime("%Y-%m-%d %H:%M:%S")})
        restored += 1
        progress(f"Put back {n:,} of {len(plan.ready):,}", n / len(plan.ready))
    for d in holding_dirs:   # tidy now-empty folders inside the holding batch folder
        for dirpath, dirnames, filenames in os.walk(d, topdown=False):
            if dirpath != d and not dirnames and not filenames:
                os.rmdir(dirpath)
    return {"batch": plan.batch, "restored": restored, "failed": failed}
