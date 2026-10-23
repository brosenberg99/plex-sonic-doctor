"""Which recording a file really is, from AcoustID's fingerprint database, to find tracks whose
tags are missing or wrong. This only finds them: the tags are fixed in MusicBrainz Picard.

The one part of the app that sends anything off this computer, and only once an AcoustID key is
set: each looked-up file's fingerprint (the first two minutes, as numbers) and its length. No file
names, paths or tags. Answers are saved in the fingerprint cache, keyed by size and modification
time, so a file is looked up once; the verdicts are worked out again each time from the saved
answers and the current tags.
"""
import base64
import csv
import difflib
import json
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .fingerprint import MIN_ITEMS, low_information

LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
SIGN_UP_URL = "https://acoustid.org/new-application"
MIN_INTERVAL = 0.34          # AcoustID asks for at most 3 requests a second
CONFIDENT = 0.8              # match score: tested on 700 tracks, wrong matches were rare above this
WEAK = 0.5                   # below this, treated as no match
KEEP_RECORDINGS = 5
KEEP_ALBUMS = 12
GIVE_UP_AFTER = 10           # failed requests in a row: AcoustID or the network is down

# MISSING: the song is identified and its tags are empty or messy. DISAGREE: AcoustID is sure,
# but names it differently (a mix-up, or just a translated title).
MISSING, DISAGREE, UNSURE, NOT_FOUND, LOOKS_RIGHT = (
    "Identified, needs tags", "Named differently", "Unsure", "Not in AcoustID", "Tags look right")
VERDICTS = [MISSING, DISAGREE, UNSURE, NOT_FOUND, LOOKS_RIGHT]
ISSUES = [MISSING, DISAGREE, UNSURE, NOT_FOUND]

NO_ARTIST, NO_ALBUM, NO_TITLE, FILE_NAME_TITLE, VIDEO_TITLE = (
    "no artist", "no album", "no real title", "title looks like a file name", "title from a video")
PROBLEMS = [NO_ARTIST, NO_ALBUM, NO_TITLE, FILE_NAME_TITLE, VIDEO_TITLE]

# Plex shows "[Unknown Artist]" and "[Unknown Album]" for empty tags; other tools leave similar marks.
_UNKNOWN = re.compile(r"^\s*[\[(<]?\s*(unknown( artist| album)?|no artist|no album)\s*[\])>]?\s*(\(.*\))?\s*$", re.I)
_GENERIC_TITLE = re.compile(r"^\s*(track|audiotrack|piste|pista|titel)\s*[\d\s._-]*$", re.I)   # not "1999" or "7"
_NUMBER_TITLE = re.compile(r"^[\d\s._-]*$")     # a number is a real title ("1999") only when the tags back it up
_FILE_NAME_TITLE =re.compile(r"\.(mp3|m4a|flac|wav|aac|ogg|wma)\s*$|^\d{1,3}\s*[-_.]\s*\S|\S_\S", re.I)
_VIDEO_TITLE = re.compile(r"official (music |lyric )?video|\blyrics?\b|\[(hd|hq|4k)\]|\((hd|hq|audio)\)|"
                          r"\bvisuali[sz]er\b|\bfull album\b", re.I)


class BadKey(Exception):
    """AcoustID didn't accept the key: no point looking up anything else."""


# ---------------------------------------------------------------- tags that look wrong

def unknown(value: str) -> bool:
    return not (value or "").strip() or bool(_UNKNOWN.match(value))


def tag_problems(t: dict) -> List[str]:
    """What's visibly wrong with a track's tags, without looking anything up."""
    out = []
    if unknown(t.get("artist")):
        out.append(NO_ARTIST)
    if unknown(t.get("album")):
        out.append(NO_ALBUM)
    title = t.get("title") or ""
    if unknown(title) or _GENERIC_TITLE.match(title):
        out.append(NO_TITLE)
    elif _FILE_NAME_TITLE.search(title):
        out.append(FILE_NAME_TITLE)
    if _VIDEO_TITLE.search(title):
        out.append(VIDEO_TITLE)
    return out


# ---------------------------------------------------------------- fingerprints in AcoustID's format

def _pack(values: List[int], width: int) -> bytes:
    acc = nbits = 0
    out = bytearray()
    for v in values:
        acc |= v << nbits
        nbits += width
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8
    if nbits:
        out.append(acc)
    return bytes(out)


def compress(raw, algorithm: int = 1) -> str:
    """A raw Chromaprint fingerprint as the compressed text fpcalc prints and AcoustID takes.
    Each item is stored as the gaps between the bits that changed since the item before."""
    normal, exceptional = [], []
    last = 0
    for h in (int(x) for x in raw):
        value, last = h ^ last, h
        prev_bit = 0
        while value:
            bit = (value & -value).bit_length()
            gap = bit - prev_bit
            if gap >= 7:
                normal.append(7)
                exceptional.append(gap - 7)
            else:
                normal.append(gap)
            prev_bit = bit
            value &= value - 1
        normal.append(0)
    n = len(raw)
    head = bytes([algorithm, (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF])
    return base64.urlsafe_b64encode(head + _pack(normal, 3) + _pack(exceptional, 5)).rstrip(b"=").decode()


# ---------------------------------------------------------------- asking AcoustID

def lookup(key: str, fingerprint: str, duration: float, timeout: float = 30) -> dict:
    body = urllib.parse.urlencode({"client": key, "duration": int(round(duration)), "fingerprint": fingerprint,
                                   "meta": "recordings releasegroups compress", "format": "json"}).encode()
    req = urllib.request.Request(LOOKUP_URL, data=body, headers={"User-Agent": "PlexSonicDoctor"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:      # AcoustID explains errors in a JSON body
        try:
            return json.loads(e.read())
        except ValueError:
            raise e


def summarise(answer: dict) -> List[dict]:
    """The best few recordings in an answer: score, MusicBrainz id, title, artists, albums."""
    best: Dict[str, dict] = {}
    for result in answer.get("results") or []:
        for rec in result.get("recordings") or []:
            if not rec.get("title") or not rec.get("id"):
                continue
            albums = [{"id": g.get("id", ""), "title": g.get("title", ""), "type": g.get("type") or "",
                       "secondary": g.get("secondarytypes") or []}
                      for g in rec.get("releasegroups") or [] if g.get("title")]
            m = {"score": round(float(result.get("score") or 0), 3), "id": rec["id"], "title": rec["title"],
                 "artists": [a.get("name", "") for a in rec.get("artists") or [] if a.get("name")],
                 "albums": albums[:KEEP_ALBUMS]}
            if rec["id"] not in best or best[rec["id"]]["score"] < m["score"]:
                best[rec["id"]] = m
    # among equal scores, the recording on more releases is usually the canonical one
    ranked = sorted(best.values(), key=lambda m: (-m["score"], -len(m["albums"])))
    return ranked[:KEEP_RECORDINGS]


def ensure_lookups(conn, key: str, tracks: List[dict], progress=lambda m, f=-1: None, should_stop=lambda: False,
                   fetch: Callable = lookup, sleep: Callable = time.sleep, clock: Callable = time.monotonic) -> int:
    """Look up every given track that has a fingerprint and no saved answer for its current
    fingerprint. Saves as it goes, so it can be stopped and resumed. Returns how many were looked up."""
    fps = {f: (s, m, start) for f, s, m, start in conn.execute("select file, size, mtime, start from fp where start is not null")}
    done_before = {f: (s, m) for f, s, m in conn.execute("select file, size, mtime from acoustid")}
    todo = []
    for t in tracks:
        fp = fps.get(t["file"])
        if fp is None or done_before.get(t["file"]) == fp[:2] or not t.get("duration"):
            continue
        raw = np.frombuffer(fp[2], "<u4")
        if len(raw) < MIN_ITEMS or low_information(raw):     # silence matches nothing useful
            continue
        todo.append((t, fp, raw))

    looked_up = failures = 0
    last_request = -1e9
    for n, (t, (size, mtime, _), raw) in enumerate(todo, start=1):
        if should_stop():
            break
        wait = MIN_INTERVAL - (clock() - last_request)
        if wait > 0:
            sleep(wait)
        last_request = clock()
        try:
            answer = fetch(key, compress(raw), t["duration"])
        except (OSError, ValueError) as e:       # network trouble: try this file again next time
            failures += 1
            if failures >= GIVE_UP_AFTER:
                raise RuntimeError(f"AcoustID isn't answering ({type(e).__name__}: {e}). "
                                   f"Try again later; what's done so far is saved.")
            sleep(min(30, 2 ** failures))
            continue
        error = (answer.get("error") or {}) if answer.get("status") != "ok" else {}
        code = error.get("code")
        if code in (4, 6):
            raise BadKey(f"AcoustID didn't accept the key: {error.get('message', 'invalid API key')}. "
                         f"Check it in Settings, or get one at {SIGN_UP_URL}")
        if answer.get("status") != "ok" and code not in (3, 8):      # busy or rate-limited: back off, retry later
            failures += 1
            if failures >= GIVE_UP_AFTER:
                raise RuntimeError(f"AcoustID keeps refusing requests: {error.get('message', 'unknown error')}. "
                                   f"Try again later; what's done so far is saved.")
            sleep(min(30, 2 ** failures))
            continue
        failures = 0
        matches = summarise(answer) if answer.get("status") == "ok" else []
        conn.execute("insert or replace into acoustid values (?,?,?,?,?,?)",
                     (t["file"], size, mtime, time.strftime("%Y-%m-%d %H:%M"), json.dumps(matches),
                      None if answer.get("status") == "ok" else error.get("message", "rejected")))
        looked_up += 1
        if looked_up % 20 == 0:
            conn.commit()
        if n % 10 == 0 or n == len(todo):
            progress(f"Looking up tracks in AcoustID: {n:,} of {len(todo):,}", n / len(todo))
    conn.commit()
    return looked_up


def load_lookups(conn) -> Dict[str, List[dict]]:
    """Saved answers that still belong to the file's current fingerprint."""
    fps = {f: (s, m) for f, s, m in conn.execute("select file, size, mtime from fp where start is not null")}
    return {f: json.loads(matches) for f, s, m, matches, err in
            conn.execute("select file, size, mtime, matches, error from acoustid")
            if not err and fps.get(f) == (s, m)}


# ---------------------------------------------------------------- comparing with the tags

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[(\[{].*?[)\]}]", " ", s)                       # (Remastered 2011), [Live]...
    s = re.sub(r"\s(feat\.?|ft\.?|featuring)\s.*$", " ", s)
    s = re.sub(r"^the\s+", "", s.strip()).replace("&", "and")
    return re.sub(r"[\W_]+", "", s)


def same_name(a: str, b: str) -> bool:
    """Equal after ignoring case, accents, brackets and "feat." credits; long names may differ a
    little (catalogue numbers like "K. 330" vs "K. 300h/330")."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    return min(len(na), len(nb)) >= 12 and difflib.SequenceMatcher(None, na, nb).ratio() >= 0.9


def credit(m: dict) -> str:
    return " & ".join(m["artists"])


def artist_agrees(t: dict, m: dict) -> bool:
    if unknown(t.get("artist")):
        return False
    names = m["artists"] + [credit(m)]
    tag_names = [t.get("artist") or "", t.get("album_artist") or ""]
    for tag in tag_names:
        nt = _norm(tag)
        for name in names:
            nn = _norm(name)
            if same_name(tag, name) or (len(nn) >= 4 and nn in nt) or (len(nt) >= 4 and nt in nn):
                return True
    return False


def title_agrees(t: dict, m: dict) -> bool:
    return not unknown(t.get("title")) and same_name(t["title"], m["title"])


_PART_SPLIT = re.compile(r"\s+[-–—/|]\s+|\s*〜")
_LEADING_NUMBER = re.compile(r"^\d{1,3}\s*[.)]\s*")


def _parts(title: str) -> List[str]:
    return [_LEADING_NUMBER.sub("", p) for p in _PART_SPLIT.split(title or "") if p.strip()]


def title_part_agrees(t: dict, m: dict) -> bool:
    """A part of one title is the other: "Muse - Apocalypse Please" and "Apocalypse Please",
    "37 - Sky Pillar", "Lothlórien / Lament for Gandalf" and "Lothlorien"."""
    return any(same_name(a, b) for a in _parts(t.get("title")) for b in _parts(m["title"]))


_ALBUM_RANK = {"Album": 0, "EP": 1, "Single": 2}


def best_album(m: dict, current: str = "") -> Optional[dict]:
    """The album this recording most likely belongs to: the one the tags already name, else a
    plain studio album before EPs, singles, compilations and live albums."""
    if not m["albums"]:
        return None
    for a in m["albums"]:
        if current and not unknown(current) and same_name(current, a["title"]):
            return a
    return min(m["albums"], key=lambda a: (bool(a["secondary"]), _ALBUM_RANK.get(a["type"], 3)))


def judge(t: dict, matches: List[dict]) -> Tuple[str, Optional[dict]]:
    """(verdict, the match it's based on) for one looked-up track. Only a confident match can
    overrule tags that look fine; a weak match or none says nothing against them."""
    problems = tag_problems(t)
    usable = [m for m in matches if m["score"] >= WEAK]
    if not usable:
        return (NOT_FOUND if problems else LOOKS_RIGHT), None
    agreeing = [m for m in usable if title_agrees(t, m) and artist_agrees(t, m)]
    if agreeing:                     # the tags back up the match, even a weaker one
        return (MISSING if problems else LOOKS_RIGHT), agreeing[0]
    confident = [m for m in usable if m["score"] >= CONFIDENT]
    if not confident:
        return (UNSURE if problems else LOOKS_RIGHT), (usable[0] if problems else None)
    # The right song with messy tags: "Artist - Song (Official Audio)", a game's name as the artist...
    same_song = [m for m in confident if title_agrees(t, m) or title_part_agrees(t, m)]
    if same_song:
        return MISSING, same_song[0]
    best = confident[0]
    if {NO_TITLE, FILE_NAME_TITLE} & set(problems) or _NUMBER_TITLE.match(t.get("title") or ""):
        # the title says nothing ("052", "Track 01"), so the matches must agree among themselves
        rivals = {(_norm(m["title"]), _norm(credit(m))) for m in confident}
        return (MISSING if len(rivals) == 1 else UNSURE), best
    return DISAGREE, best


# ---------------------------------------------------------------- the report

def report_rows(tracks: List[dict], lookups: Dict[str, List[dict]]) -> List[dict]:
    rows = []
    for t in tracks:
        matches = lookups.get(t["file"])
        if matches is None:
            continue
        verdict, m = judge(t, matches)
        album = best_album(m, t.get("album", "")) if m else None
        rows.append({"file": t["file"], "verdict": verdict, "problems": tag_problems(t),
                     "score": m["score"] if m else (matches[0]["score"] if matches else 0),
                     "recording": m["id"] if m else "", "title": m["title"] if m else "",
                     "artist": credit(m) if m else "", "album": album["title"] if album else "",
                     "album_type": (album["type"] + (" (" + ", ".join(album["secondary"]) + ")" if album["secondary"] else "")
                                    if album else ""),
                     # every album the match appears on, for working out a whole folder's album
                     "albums": [[a["id"], a["title"]] for a in m["albums"]] if m else [],
                     "others": [{"title": o["title"], "artist": credit(o), "score": o["score"]}
                                for o in matches if m and o["id"] != m["id"] and o["score"] >= WEAK][:3]})
    return rows


def write_report(tracks: List[dict], lookups: Dict[str, List[dict]], out_dir: str) -> dict:
    """tag_issues.csv (every track whose tags need work) and tags.json for the GUI."""
    rows = report_rows(tracks, lookups)
    by_file = {t["file"]: t for t in tracks}
    stats = {"looked_up": len(rows), "verdicts": {v: sum(1 for r in rows if r["verdict"] == v) for v in VERDICTS},
             "suspect_tracks": sum(1 for t in tracks if tag_problems(t)),
             "suspect_looked_up": sum(1 for r in rows if r["problems"])}
    os.makedirs(out_dir, exist_ok=True)
    order = {v: n for n, v in enumerate(VERDICTS)}
    with open(os.path.join(out_dir, "tag_issues.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["verdict", "tag_problems", "title", "artist", "album", "identified_title", "identified_artist",
                    "identified_album", "album_type", "match_score", "musicbrainz_recording", "file", "plex_id"])
        for r in sorted(rows, key=lambda r: (order[r["verdict"]], r["file"].lower())):
            if r["verdict"] == LOOKS_RIGHT:
                continue
            t = by_file[r["file"]]
            w.writerow([r["verdict"], "; ".join(r["problems"]), t["title"], t["artist"], t["album"], r["title"],
                        r["artist"], r["album"], r["album_type"], f"{r['score']:.2f}" if r["recording"] else "",
                        f"https://musicbrainz.org/recording/{r['recording']}" if r["recording"] else "",
                        t["file"], t["id"]])
    data = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "rows": rows, "stats": stats}
    path = os.path.join(out_dir, "tags.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)
    return data


def summary_text(stats: dict) -> str:
    v = stats["verdicts"]
    lines = [f"Looked up in AcoustID: {stats['looked_up']:,} tracks "
             f"({stats['suspect_looked_up']:,} of the {stats['suspect_tracks']:,} whose tags look wrong)"]
    lines += [f"  {name}: {v.get(name, 0):,}" for name in VERDICTS]
    return "\n".join(lines)


# ---------------------------------------------------------------- MusicBrainz Picard

def picard_command(configured: str = "") -> Optional[List[str]]:
    """How to start Picard here, or None if it isn't installed."""
    if configured:
        return [configured] if os.path.exists(configured) or shutil.which(configured) else None
    found = shutil.which("picard")
    if found:
        return [found]
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            exe = os.path.join(base or "", "MusicBrainz Picard", "picard.exe")
            if base and os.path.exists(exe):
                return [exe]
    if sys.platform == "darwin" and os.path.isdir("/Applications/MusicBrainz Picard.app"):
        return ["open", "-a", "MusicBrainz Picard"]
    return None


def picard_help() -> str:
    if sys.platform == "win32":
        return "Install it with  winget install MusicBrainz.Picard  (or from picard.musicbrainz.org)."
    if sys.platform == "darwin":
        return "Install it with  brew install --cask musicbrainz-picard  (or from picard.musicbrainz.org)."
    return "Install it from your package manager (usually 'picard') or picard.musicbrainz.org."
