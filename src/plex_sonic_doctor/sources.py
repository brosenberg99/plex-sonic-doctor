"""Where a file came from (iTunes Store, Bandcamp, Amazon...), from the marks stores and tools
leave in its tags, with the folder as a fallback. Read only for the tracks in the reports.

Only the verdict is kept, never the tags: an iTunes purchase carries the buyer's Apple ID.
Verdicts are saved in the fingerprint cache, keyed by size and modification time.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable, List, Optional

ITUNES, BANDCAMP, AMAZON, YOUTUBE, SOUNDCLOUD, CD_RIP, UNKNOWN = (
    "iTunes Store", "Bandcamp", "Amazon", "YouTube", "SoundCloud", "CD rip", "Unknown")
SOURCES = (ITUNES, BANDCAMP, AMAZON, YOUTUBE, SOUNDCLOUD, CD_RIP, UNKNOWN)

# MP4 atoms only a store purchase has: purchase date, Apple ID, owner's name
_PURCHASE_ATOMS = {"purd", "apID", "ownr"}
# Marks in tag names and text, checked in this order (a CD rip can mention a store's URL in a comment)
_MARKS = [
    (BANDCAMP, re.compile(r"bandcamp\.com", re.I)),
    (AMAZON, re.compile(r"amazon\.com song id|www\.amazon\.com|amazon mp3", re.I)),
    (YOUTUBE, re.compile(r"youtube\.com/|youtu\.be/|provided to youtube", re.I)),
    (SOUNDCLOUD, re.compile(r"soundcloud\.com", re.I)),
    (CD_RIP, re.compile(r"itunes_cddb|exact audio copy|\beac\b|dbpoweramp|\bxld\b|cuetools|"
                        r"windows media player|\biTunes v?\d", re.I)),
]
_FOLDER_MARKS = [(BANDCAMP, "bandcamp"), (AMAZON, "amazon"), (YOUTUBE, "youtube"), (SOUNDCLOUD, "soundcloud")]
_MAX_TEXT = 2000          # per tag: enough for any comment, and skips long lyrics


def _tag_text(tags) -> List[str]:
    """Tag names and short text values, skipping pictures and other binary data."""
    out = []
    for key in list(tags.keys()):
        out.append(str(key))
        try:
            value = tags[key]
        except (KeyError, ValueError):
            continue
        values = value if isinstance(value, list) else [value]
        for v in values:
            if isinstance(v, (bytes, bytearray)) or type(v).__name__ in ("MP4Cover", "APIC", "Picture"):
                continue
            if hasattr(v, "text") and not isinstance(v, str):      # ID3 frames
                v = " ".join(str(x) for x in (v.text if isinstance(v.text, list) else [v.text]))
            elif hasattr(v, "url"):
                v = v.url
            v = str(v)
            if len(v) <= _MAX_TEXT:
                out.append(v)
    return out


def classify(path: str) -> str:
    """The source of one file. Unreadable or untagged files fall back to the folder name."""
    found = None
    try:
        import mutagen
        try:
            f = mutagen.File(path)
            tags = getattr(f, "tags", None) if f is not None else None
        except Exception:
            tags = None
        if tags is None:       # audio too damaged to parse: an ID3 tag at the front still reads
            from mutagen.id3 import ID3
            tags = ID3(path)
        if tags is not None:
            names = {str(k) for k in tags.keys()}
            if names & _PURCHASE_ATOMS:
                return ITUNES
            text = "\n".join(_tag_text(tags))
            found = next((name for name, rx in _MARKS if rx.search(text)), None)
    except Exception:      # damaged files are common in these lists; the folder may still say
        pass
    if found and found != CD_RIP:
        return found
    low = path.lower()
    by_folder = next((name for name, word in _FOLDER_MARKS if word in low), None)
    return by_folder or found or UNKNOWN


def ensure_sources(conn, files: Iterable[str], workers: int = 8, recheck: bool = True,
                   progress=lambda m, f=-1: None, should_stop=lambda: False) -> int:
    """Classify files that aren't cached. With recheck, cached files whose size or date changed
    are done again (that means looking at every file, slow over a network share)."""
    cached = {f: (s, m) for f, s, m in conn.execute("select file, size, mtime from source")}
    todo = []
    for path in sorted(set(files)):
        if path in cached and not recheck:
            continue
        try:
            st = os.stat(path)
            if cached.get(path) == (st.st_size, st.st_mtime):
                continue
            todo.append((path, st.st_size, st.st_mtime))
        except OSError:
            if path not in cached:
                todo.append((path, -1, 0))

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(classify, path): (path, size, mtime) for path, size, mtime in todo}
        for fut in as_completed(futures):
            path, size, mtime = futures[fut]
            conn.execute("insert or replace into source values (?,?,?,?)", (path, size, mtime, fut.result()))
            done += 1
            if done % 200 == 0 or done == len(todo):
                conn.commit()
                progress(f"Reading where files came from: {done:,} of {len(todo):,}", done / max(len(todo), 1))
            if should_stop():
                for f in futures:
                    f.cancel()
                break
    conn.commit()
    return done


def load_sources(conn, files: Optional[Iterable[str]] = None) -> Dict[str, str]:
    rows = {f: s for f, s in conn.execute("select file, source from source")}
    return rows if files is None else {f: rows[f] for f in files if f in rows}
