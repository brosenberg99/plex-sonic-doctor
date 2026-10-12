"""Find and read Plex's sonic analysis files.

Plex (with a Plex Pass) stores a 52-number "sound profile" for every analysed track in
    <Plex data folder>/Plug-in Support/Databases/Music Analysis <N>/
        Track.tree   fixed 212-byte slots: uint32 "used" flag + 52 float32, little-endian
        Mapping.db   SQLite: mappings(id = Plex item id, external_id = slot, metadata_type 10 = track)
The format was reverse-engineered by knoopx (https://gist.github.com/knoopx/567b74342ad82eac1b97334caebb1d7a);
it is unofficial and may change between Plex versions.

The live files are never opened: they are copied first, because Plex writes to them.
"""
import glob
import os
import re
import shutil
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

SLOT_FLOATS = 53   # 1 flag word + 52 floats
TRACK = 10


def candidate_data_dirs() -> List[str]:
    """Usual "Plex Media Server" data folders on the machine running this tool."""
    out = []
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Plex, Inc.\Plex Media Server") as k:
                custom, _ = winreg.QueryValueEx(k, "LocalAppDataPath")
                out.append(os.path.join(custom, "Plex Media Server"))
        except OSError:
            pass
        if os.environ.get("LOCALAPPDATA"):
            out.append(os.path.join(os.environ["LOCALAPPDATA"], "Plex Media Server"))
    elif sys.platform == "darwin":
        out.append(os.path.expanduser("~/Library/Application Support/Plex Media Server"))
    else:
        if os.environ.get("PLEX_HOME"):
            out.append(os.path.join(os.environ["PLEX_HOME"], "Library/Application Support/Plex Media Server"))
        out += [
            "/var/lib/plexmediaserver/Library/Application Support/Plex Media Server",          # Debian/Ubuntu/Fedora
            "/var/snap/plexmediaserver/common/Library/Application Support/Plex Media Server",  # Snap
            "/config/Library/Application Support/Plex Media Server",                          # Docker (run inside)
            "/volume1/PlexMediaServer/AppData/Plex Media Server",                             # Synology DSM 7
            "/volume1/Plex/Library/Application Support/Plex Media Server",                    # Synology DSM 6
            "/share/CACHEDEV1_DATA/.qpkg/PlexMediaServer/Library/Plex Media Server",          # QNAP
            "/mnt/user/appdata/plex/Library/Application Support/Plex Media Server",           # unRAID
        ]
    return out


def find_music_analysis(folder: str) -> Optional[str]:
    """Given a Plex data folder (or any folder above/inside it), return its newest
    "Music Analysis N" folder that has both files, or None."""
    folder = os.path.abspath(folder)
    patterns = [
        os.path.join(folder, "Music Analysis *"),
        os.path.join(folder, "Databases", "Music Analysis *"),
        os.path.join(folder, "Plug-in Support", "Databases", "Music Analysis *"),
        os.path.join(folder, "Plex Media Server", "Plug-in Support", "Databases", "Music Analysis *"),
        os.path.join(folder, "Library", "Application Support", "Plex Media Server", "Plug-in Support",
                     "Databases", "Music Analysis *"),
    ]
    if os.path.basename(folder).startswith("Music Analysis"):
        patterns.insert(0, folder)
    found = []
    for pat in patterns:
        for d in glob.glob(pat):
            if os.path.isfile(os.path.join(d, "Track.tree")) and os.path.isfile(os.path.join(d, "Mapping.db")):
                m = re.search(r"(\d+)$", d)
                found.append((int(m.group(1)) if m else 0, d))
    return max(found)[1] if found else None


def auto_find() -> Optional[str]:
    for d in candidate_data_dirs():
        hit = find_music_analysis(d)
        if hit:
            return hit
    return None


def copy_sonic_files(src: str, dst: str, wait: float = 30) -> None:
    """Copy the two files; copy again if Plex rewrote one while we copied."""
    os.makedirs(dst, exist_ok=True)
    names = ["Track.tree", "Mapping.db"]
    stamp = lambda: {n: (os.path.getsize(os.path.join(src, n)), os.path.getmtime(os.path.join(src, n))) for n in names}
    for _ in range(3):
        before = stamp()
        for n in names:
            shutil.copyfile(os.path.join(src, n), os.path.join(dst, n))
        if stamp() == before:
            return
        time.sleep(wait)
    raise RuntimeError("Plex kept changing its analysis files during the copy; try again later")


def load_profiles(sonic_dir: str) -> Tuple[np.ndarray, Dict[int, int]]:
    """Returns (profiles[slot, 52], {plex track id: slot}) for occupied track slots."""
    raw = np.fromfile(os.path.join(sonic_dir, "Track.tree"), dtype="<f4")
    if raw.size % SLOT_FLOATS:
        raise ValueError("Track.tree isn't in the expected format (Plex may have changed it)")
    raw = raw.reshape(-1, SLOT_FLOATS)
    flags = raw[:, 0].view("<u4")
    db = sqlite3.connect("file:" + os.path.join(sonic_dir, "Mapping.db") + "?mode=ro", uri=True)
    try:
        slot_of = {i: s for i, s in db.execute("select id, external_id from mappings where metadata_type=?", (TRACK,))
                   if 0 <= s < len(flags) and flags[s] == 1}
    finally:
        db.close()
    return raw[:, 1:], slot_of


def unit_vectors(profiles: np.ndarray, slots: List[int]) -> np.ndarray:
    v = profiles[slots].astype(np.float64)
    n = np.linalg.norm(v, axis=1, keepdims=True)
    n[n == 0] = 1
    return v / n


def candidate_pairs(units: np.ndarray, scale: float, max_distance: float, chunk: int = 512):
    """All index pairs (i < j) whose Plex-equivalent distance (scale x unit-vector distance) is under max_distance."""
    min_cos = 1 - (max_distance / scale) ** 2 / 2
    u32 = units.astype(np.float32)
    out_i, out_j = [], []
    for start in range(0, len(u32), chunk):
        sims = u32[start:start + chunk] @ u32.T
        rows, cols = np.nonzero(sims >= min_cos - 1e-6)
        rows = rows + start
        keep = cols > rows
        out_i.append(rows[keep])
        out_j.append(cols[keep])
    if not out_i:
        return np.array([], int), np.array([], int), np.array([])
    i, j = np.concatenate(out_i), np.concatenate(out_j)
    d = scale * np.linalg.norm(units[i] - units[j], axis=1)
    keep = d < max_distance
    return i[keep], j[keep], d[keep]


def fit_scale(units_a: np.ndarray, units_b: np.ndarray, plex_distances: np.ndarray) -> float:
    """Least-squares factor between our unit-vector distance and Plex's reported distance."""
    d = np.linalg.norm(units_a - units_b, axis=1)
    return float((d @ plex_distances) / (d @ d)) if len(d) else 1.08
