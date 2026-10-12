"""A quick look at the files Plex couldn't analyse: is it audio at all, is it still encrypted,
does it decode? Plex analyses a whole album at once, so one such file stops every track
in its album from getting sonic analysis.

Results are saved in the fingerprint cache, keyed by size and modification time, so only new
or changed files are looked at again.
"""
import os
import re
import struct
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from .fingerprint import _run

DECODE_SECONDS = 60
SEVERITY = {"missing from disk": "Check"}     # everything else found here is "Broken"

UNFINISHED_ITUNES = ("unfinished iTunes download",
                     "the audio is still encrypted by Apple's FairPlay, and the file holds no key to unlock it, "
                     "so nothing can play it. Download it again from your iTunes purchases")
DRM = ("copy-protected (DRM)", "the audio is locked to an Apple account (FairPlay); Plex can't play these")
OTHER_DRM = ("copy-protected (DRM)", "the audio is encrypted (copy protection); Plex can't play it")


def _boxes(f, start: int, end: int):
    """(type, payload start, box end) for each MP4 box between start and end."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        head = f.read(8)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head)
        body = pos + 8
        if size == 1:
            size, body = struct.unpack(">Q", f.read(8))[0], pos + 16
        elif size == 0:
            size = end - pos
        if size < body - pos:
            return
        yield kind, body, pos + size
        pos += size


def _find(f, start, end, path):
    """The payload range of the first box along path (e.g. [b"moov", b"trak", ...])."""
    for kind, body, stop in _boxes(f, start, end):
        if kind == path[0]:
            return (body, stop) if len(path) == 1 else _find(f, body, stop, path[1:])
    return None


def mp4_protection(path: str) -> Optional[Tuple[str, str]]:
    """For an MP4/M4A: None if the audio isn't encrypted, else the problem. An iTunes download
    that never finished keeps the encrypted audio and FairPlay's marker box, but has a blank
    space where the key information belongs (its finished album-mates carry purchase info there
    instead). A real copy-protected purchase has that key information."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        end = f.tell()
        moov = _find(f, 0, end, [b"moov"])
        if not moov:
            return None
        for kind, body, stop in _boxes(f, *moov):
            if kind != b"trak":
                continue
            stsd = _find(f, body, stop, [b"mdia", b"minf", b"stbl", b"stsd"])
            if not stsd:
                continue
            for entry, ebody, estop in _boxes(f, stsd[0] + 8, stsd[1]):   # skip version/flags and count
                if entry == b"enca":
                    return OTHER_DRM
                if entry != b"drms":
                    continue
                sinf = _find(f, ebody + 28, estop, [b"sinf"])            # after the audio sample entry fields
                if sinf and _find(f, sinf[0], sinf[1], [b"schi"]):
                    return DRM
                return UNFINISHED_ITUNES
    return None


def _web_page_title(head: bytes) -> str:
    m = re.search(rb"<title>([^<]{1,80})</title>", head, re.I)
    return m.group(1).decode("utf-8", "replace").strip() if m else ""


def check_file(ffmpeg: str, path: str) -> Optional[Tuple[str, str]]:
    """None if the file looks like playable audio, else (problem, details)."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(4096)
    except FileNotFoundError:
        return ("missing from disk", "Plex lists this file but it isn't there (moved or deleted outside this tool?)")
    except OSError as e:
        return ("couldn't be opened", f"{type(e).__name__}: {e}"[:200])
    if size == 0:
        return ("empty file", "the file is 0 bytes")
    start = head.lstrip()[:200].lower()
    if start.startswith((b"<?xml", b"<!doctype", b"<html", b"<head", b"<body")):
        title = _web_page_title(head)
        return ("not audio", f"it's a saved web page{f' (“{title}”)' if title else ''}, not a song")
    if head[4:8] == b"ftyp":
        try:
            found = mp4_protection(path)
        except OSError:
            found = None
        if found:
            return found
    out = _run([ffmpeg, "-v", "error", "-i", path, "-map", "0:a:0", "-t", str(DECODE_SECONDS), "-f", "null", "-"],
               timeout=600)
    if out.returncode != 0:
        from .analysis import explain_open_error
        err = out.stderr.decode("utf-8", "replace").strip() or "ffmpeg couldn't decode the file"
        return ("couldn't be decoded", explain_open_error(err))
    return None


def run_file_checks(conn, ffmpeg: str, tracks: List[dict], workers: int = 4,
                    progress=lambda m, f=-1: None, should_stop=lambda: False) -> int:
    """Check the given tracks' files (skipping unchanged ones already checked). Saves as it goes."""
    cached = {f: (s, m) for f, s, m in conn.execute("select file, size, mtime from filecheck")}
    todo = []
    for t in tracks:
        try:
            st = os.stat(t["file"])
            if cached.get(t["file"]) == (st.st_size, st.st_mtime):
                continue
            todo.append((t["file"], st.st_size, st.st_mtime))
        except OSError:
            todo.append((t["file"], -1, 0))

    def one(item):
        path, size, mtime = item
        try:
            found = check_file(ffmpeg, path)
        except Exception:           # e.g. no ffmpeg: says nothing about the file, so look again next scan
            found, size = None, -1
        return path, size, mtime, found

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, item) for item in todo]
        for fut in as_completed(futures):
            path, size, mtime, found = fut.result()
            conn.execute("insert or replace into filecheck values (?,?,?,?,?)",
                         (path, size, mtime, found[0] if found else None, found[1] if found else None))
            done += 1
            if done % 50 == 0 or done == len(todo):
                conn.commit()
                progress(f"Checking files Plex couldn't analyse: {done:,} of {len(todo):,}", done / max(len(todo), 1))
            if should_stop():
                for f in futures:
                    f.cancel()
                break
    conn.commit()
    return done


def load_file_checks(conn) -> Dict[str, Tuple[str, str]]:
    """{file: (problem, details)} for every checked file with a problem."""
    return {f: (p, d) for f, p, d in conn.execute("select file, problem, details from filecheck") if p}
