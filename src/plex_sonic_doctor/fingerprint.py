"""Audio fingerprints (Chromaprint) and the optional full damage check.

Two ways to make the same fingerprint, whichever this computer has:
  * ffmpeg built with Chromaprint (e.g. the Windows "full" builds): ffmpeg does it all.
  * any ffmpeg + fpcalc (Chromaprint's own tool; Homebrew "chromaprint", Debian/Ubuntu
    "libchromaprint-tools", or acoustid.org): ffmpeg cuts out the section, fpcalc fingerprints it.
Both give the same fingerprints (checked: 99.9-100% agreement), so caches and thresholds are shared.
"""
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

START_SECONDS = 120          # fingerprint the first two minutes...
MID_SECONDS = 60             # ...and one minute from the middle of longer tracks
MID_MIN_DURATION = 240
MIN_ITEMS = 40               # ~5 s; anything shorter can't be judged
MAX_OFFSET = 80              # alignment search in fingerprint items (~0.124 s each), ~10 s
ITEMS_PER_SECOND = 7.9
LOW_INFO_UNIQUE = 0.30       # silence ~0.001 distinct values, music ~0.98
LOW_INFO_BITFLIPS = 1.0      # silence 0 bits changing per step, music ~4

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0   # no console windows popping up under the GUI


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW, **kw)


@dataclass
class Backend:
    kind: str          # "ffmpeg" (built-in Chromaprint) or "fpcalc"
    ffmpeg: Optional[str]
    fpcalc: Optional[str]

    @property
    def can_seek(self) -> bool:
        return bool(self.ffmpeg)


def tool_works(cmd: str, arg: str) -> bool:
    try:
        return _run([cmd, arg], timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ffmpeg_has_chromaprint(ffmpeg: str) -> bool:
    try:
        out = _run([ffmpeg, "-hide_banner", "-muxers"], timeout=30)
        return b"chromaprint" in out.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def detect(ffmpeg: str = "ffmpeg", fpcalc: str = "fpcalc") -> Tuple[Optional[Backend], str]:
    """Pick a fingerprint method. Returns (backend or None, explanation)."""
    has_ffmpeg = tool_works(ffmpeg, "-version")
    if has_ffmpeg and ffmpeg_has_chromaprint(ffmpeg):
        return Backend("ffmpeg", ffmpeg, None), "ffmpeg with Chromaprint"
    has_fpcalc = tool_works(fpcalc, "-version")
    if has_fpcalc and has_ffmpeg:
        return Backend("fpcalc", ffmpeg, fpcalc), "ffmpeg + fpcalc"
    if has_fpcalc:
        return Backend("fpcalc", None, fpcalc), "fpcalc only (no ffmpeg, so only the start of each track is checked)"
    if has_ffmpeg:
        return None, "ffmpeg is installed but has no Chromaprint, and fpcalc isn't installed"
    return None, "neither ffmpeg nor fpcalc is installed"


def install_help() -> str:
    if sys.platform == "win32":
        return "Windows: run  winget install Gyan.FFmpeg  (the full build includes Chromaprint), then restart."
    if sys.platform == "darwin":
        return "macOS (Homebrew): run  brew install ffmpeg chromaprint"
    return ("Linux: install ffmpeg and Chromaprint's fpcalc, e.g.  sudo apt install ffmpeg libchromaprint-tools  "
            "(Debian/Ubuntu) or  sudo dnf install ffmpeg chromaprint-tools  (Fedora).")


# ---------------------------------------------------------------- making fingerprints

def _window(backend: Backend, path: str, seek: Optional[float], seconds: int) -> np.ndarray:
    if backend.kind == "ffmpeg":
        cmd = [backend.ffmpeg, "-v", "error"]
        if seek:
            cmd += ["-ss", f"{seek:.1f}"]
        # -ac 2: Chromaprint takes at most two channels, so surround files are mixed down first
        cmd += ["-t", str(seconds), "-i", path, "-map", "0:a:0", "-ac", "2",
                "-f", "chromaprint", "-fp_format", "raw", "-"]
        out = _run(cmd, timeout=300)
        if out.returncode != 0 or not out.stdout:
            raise RuntimeError(out.stderr.decode("utf-8", "replace").strip()[:300] or "no fingerprint")
        return np.frombuffer(out.stdout, "<u4").copy()
    if backend.ffmpeg:   # ffmpeg decodes the section, fpcalc fingerprints it from a pipe
        dec_cmd = [backend.ffmpeg, "-v", "error"] + (["-ss", f"{seek:.1f}"] if seek else []) + \
                  ["-t", str(seconds), "-i", path, "-map", "0:a:0", "-ac", "2", "-ar", "44100", "-f", "s16le", "-"]
        dec = subprocess.Popen(dec_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
        out = subprocess.run([backend.fpcalc, "-raw", "-json", "-format", "s16le", "-rate", "44100",
                              "-channels", "2", "-length", str(seconds), "-"],
                             stdin=dec.stdout, capture_output=True, timeout=300, creationflags=_NO_WINDOW)
        dec.stdout.close()
        dec_err = dec.stderr.read().decode("utf-8", "replace")
        dec.stderr.close()
        dec.wait()
        if dec.returncode != 0:
            raise RuntimeError(dec_err.strip()[:300] or "couldn't decode")
    else:
        out = _run([backend.fpcalc, "-raw", "-json", "-length", str(seconds), path], timeout=300)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode("utf-8", "replace").strip()[:300] or "no fingerprint")
    return np.array(json.loads(out.stdout)["fingerprint"], dtype=np.uint32)


def fingerprint_file(backend: Backend, path: str, duration: float):
    """(path, size, mtime, start bytes, mid bytes, error)."""
    try:
        st = os.stat(path)
        start = _window(backend, path, None, START_SECONDS)
        mid = None
        if duration >= MID_MIN_DURATION and backend.can_seek:
            mid = _window(backend, path, duration / 2 - MID_SECONDS / 2, MID_SECONDS)
        return path, st.st_size, st.st_mtime, start.astype("<u4").tobytes(), \
            None if mid is None else mid.astype("<u4").tobytes(), None
    except Exception as e:
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            size, mtime = -1, 0
        return path, size, mtime, None, None, f"{type(e).__name__}: {e}"[:400]


def open_cache(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS fp (
        file TEXT PRIMARY KEY, size INTEGER, mtime REAL, start BLOB, mid BLOB, error TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS damage (
        file TEXT PRIMARY KEY, size INTEGER, mtime REAL, errors INTEGER, first_error TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS filecheck (
        file TEXT PRIMARY KEY, size INTEGER, mtime REAL, problem TEXT, details TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS source (
        file TEXT PRIMARY KEY, size INTEGER, mtime REAL, source TEXT)""")
    return conn


def ensure_fingerprints(conn, backend: Backend, tracks: List[dict], workers: int = 8,
                        progress=lambda m, f=-1: None, should_stop=lambda: False) -> int:
    """Fingerprint tracks that aren't cached (or changed). Files that failed last time are
    retried. Returns how many were fingerprinted now."""
    cached = {f: (s, m) for f, s, m, err in conn.execute("select file, size, mtime, error from fp") if not err}
    todo = []
    for t in tracks:
        try:
            st = os.stat(t["file"])
            if cached.get(t["file"]) == (st.st_size, st.st_mtime):
                continue
        except OSError:
            pass
        todo.append(t)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fingerprint_file, backend, t["file"], t["duration"]) for t in todo]
        for fut in as_completed(futures):
            conn.execute("insert or replace into fp values (?,?,?,?,?,?)", fut.result())
            conn.commit()
            done += 1
            if done % 100 == 0 or done == len(todo):
                progress(f"Fingerprinting: {done:,} of {len(todo):,} files", done / len(todo))
            if should_stop():
                for f in futures:
                    f.cancel()
                break
    return done


def load_fingerprints(conn, files) -> Dict[str, tuple]:
    out = {}
    for f, start, mid, err in conn.execute("select file, start, mid, error from fp"):
        if f in files:
            out[f] = (np.frombuffer(start, "<u4") if start else None,
                      np.frombuffer(mid, "<u4") if mid else None, err)
    return out


# ---------------------------------------------------------------- comparing fingerprints

def fp_match(a, b) -> Optional[float]:
    """Best bit agreement over a +/- ~10 s alignment search: 0.5 = unrelated, 1.0 = identical."""
    if a is None or b is None or len(a) < MIN_ITEMS or len(b) < MIN_ITEMS:
        return None
    best = 0.0
    for off in range(-MAX_OFFSET, MAX_OFFSET + 1):
        x, y = a[max(0, off):], b[max(0, -off):]
        n = min(len(x), len(y))
        if n < MIN_ITEMS:
            continue
        best = max(best, 1 - np.bitwise_count(x[:n] ^ y[:n]).mean() / 32)
    return float(best)


def audio_match(fa, fb, same_length: bool) -> Optional[float]:
    """Lowest of the start and middle matches; None if it can't be judged."""
    if fa is None or fb is None:
        return None
    start = fp_match(fa[0], fb[0])
    if start is None:
        return None
    if same_length and fa[1] is not None and fb[1] is not None:
        mid = fp_match(fa[1], fb[1])
        if mid is not None:
            return min(start, mid)
    return start


def low_information(fp) -> bool:
    """Silence or a steady drone: such fingerprints match each other whatever the track."""
    if fp is None or len(fp) < 2:
        return False
    unique = len(np.unique(fp)) / len(fp)
    flips = np.bitwise_count(fp[1:] ^ fp[:-1]).mean()
    return unique < LOW_INFO_UNIQUE or flips < LOW_INFO_BITFLIPS


# ---------------------------------------------------------------- optional full damage check

def damage_check_file(ffmpeg: str, path: str):
    """Decode the whole file and count decoder errors. (path, size, mtime, errors, first error)."""
    try:
        st = os.stat(path)
        out = _run([ffmpeg, "-v", "error", "-i", path, "-map", "0:a:0", "-f", "null", "-"], timeout=1800)
        lines = [l for l in out.stderr.decode("utf-8", "replace").splitlines() if l.strip()]
        if out.returncode != 0 and not lines:
            lines = ["ffmpeg couldn't decode the file"]
        return path, st.st_size, st.st_mtime, len(lines), lines[0][:300] if lines else None
    except Exception as e:
        return path, -1, 0, 1, f"{type(e).__name__}: {e}"[:300]


def run_damage_check(conn, ffmpeg: str, tracks: List[dict], workers: int = 4,
                     progress=lambda m, f=-1: None, should_stop=lambda: False) -> int:
    """Decode every file (or the given ones) looking for corruption. Hours for a big library;
    results are saved as it goes, so it can be stopped and resumed."""
    cached = {f: (s, m) for f, s, m in conn.execute("select file, size, mtime from damage")}
    todo = []
    for t in tracks:
        try:
            st = os.stat(t["file"])
            if cached.get(t["file"]) == (st.st_size, st.st_mtime):
                continue
        except OSError:
            continue
        todo.append(t)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(damage_check_file, ffmpeg, t["file"]) for t in todo]
        for fut in as_completed(futures):
            conn.execute("insert or replace into damage values (?,?,?,?,?)", fut.result())
            conn.commit()
            done += 1
            if done % 50 == 0 or done == len(todo):
                progress(f"Damage check: {done:,} of {len(todo):,} files", done / max(len(todo), 1))
            if should_stop():
                for f in futures:
                    f.cancel()
                break
    return done


def load_damage(conn) -> Dict[str, Tuple[int, str]]:
    return {f: (n, e) for f, n, e in conn.execute("select file, errors, first_error from damage") if n}
