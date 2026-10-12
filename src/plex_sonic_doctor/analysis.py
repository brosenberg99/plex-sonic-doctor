"""The decisions: which copies are identical, which are different versions, which files look broken.

Pure functions over plain data (track dicts, fingerprints, Plex loudness numbers), so they can be
tested without Plex or audio files.
"""
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .config import Rules
from .filecheck import SEVERITY as FILECHECK_SEVERITY
from .fingerprint import ITEMS_PER_SECOND, START_SECONDS, audio_match, low_information

SAME_LENGTH_SECONDS = 3.0        # longer gaps: compare the start only (a middle window wouldn't line up)
SONIC_ONLY_MAX_DISTANCE = 0.01   # without a fingerprint, only near-exact sonic matches count
SONIC_ONLY_MAX_LENGTH_DIFF = 1.0
LOSSLESS = {"flac", "alac", "pcm", "wav", "aiff", "ape", "wavpack", "wv", "tta", "dsd"}

BROKEN_PEAK = 2.0                # x full scale; real audio peaks at ~1.3 at most, even lossy files
NOISE_MIN_LOUDNESS = -6.0        # LUFS: louder than nearly any mastered music...
NOISE_MAX_LRA = 0.5              # ...with almost no change in level = constant noise or static
PREVIEW_LENGTHS = (30, 60, 90)   # usual store preview-clip lengths, seconds
ALBUM_FOLDER_MAX_TRACKS = 100    # bigger folders (a loose-files root) say nothing about one album
SEVERITY_ORDER = ["Broken", "Likely broken", "Check"]
REASON_PROBLEM = "problem file"
REASON_BLOCKED = "blocked by another file in its album"
REASON_DISCARDED = "result thrown away by Plex"
REASON_UNKNOWN = "no problem found"
NOT_ANALYSED_REASONS = [REASON_PROBLEM, REASON_BLOCKED, REASON_DISCARDED, REASON_UNKNOWN]
INTENTIONAL_SILENCE = r"silen|untitled|blank|hidden|4.33|pause|nothing|empty|\bgap\b|intermission|\bspace\b"

# Words that mark a version, read only from brackets or a short trailing " - ..." part, so a song
# called "Live at Heart" isn't "live". "Remaster" is deliberately absent: loudness tells whether a
# remaster really differs (many labelled remasters measure as the same master).
VERSION_WORDS = {
    "live": r"\blive\b|\bin concert\b",
    "remix": r"\bremix|\brmx\b|\brework|\bbootleg\b|\bvip\b|\bdub\b",
    "mix": r"\bmix\b",
    "edit": r"\bedit\b|\bradio version\b|\bsingle version\b",
    "extended": r"\bextended\b",
    "instrumental": r"\binstrumental\b|\binst\b\.?|\bkaraoke\b|\boff vocal\b|\bbacking track\b",
    "a cappella": r"\ba ?cappella\b|\bacapella\b|\bvocals? only\b",
    "acoustic": r"\bacoustic\b|\bunplugged\b",
    "demo": r"\bdemo\b",
    "mono": r"\bmono\b",
    "stereo": r"\bstereo\b",
    "reprise": r"\breprise\b",
    "alternate": r"\balternate\b|\balt\.? (take|version|mix)\b|\balternative (take|version|mix)\b|\btake \d",
    "clean": r"\bclean\b|\bcensored\b",
    "cover": r"\bcover\b",
    "sped up/slowed": r"\bsped up\b|\bslowed\b|\bnightcore\b",
}
NEUTRAL_PHRASES = r"\bmix ?tape\b|\boriginal mix\b|\balbum mix\b|\balbum version\b|\boriginal version\b"


def fmt_dur(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


# ---------------------------------------------------------------- titles

def same_title(a: str, b: str) -> bool:
    """Same after dropping bracketed parts and punctuation, or one contains the other
    ("Some Band - Paper Moon (Official Video)" vs "Paper Moon")."""
    norm = lambda s: re.sub(r"[^\w]", "", re.sub(r"\(.*?\)|\[.*?\]", "", s.lower()))
    na, nb = norm(a), norm(b)
    if na == nb:
        return True
    short, long_ = sorted((na, nb), key=len)
    return len(short) >= 4 and short in long_


def version_words(title: str) -> set:
    parts = re.findall(r"[\(\[](.*?)[\)\]]", title)
    tail = title.rsplit(" - ", 1)
    if len(tail) == 2 and len(tail[1].split()) <= 4:
        parts.append(tail[1])
    text = re.sub(NEUTRAL_PHRASES, " ", " ".join(parts).lower())
    return {name for name, pat in VERSION_WORDS.items() if re.search(pat, text)}


def title_word_difference(ta: dict, tb: dict) -> Optional[str]:
    """"title says live vs no version words", or None. Only when both titles name the same song:
    in "Night Drive (Alternative)" vs "195" the second title says nothing either way."""
    wa, wb = version_words(ta["title"]), version_words(tb["title"])
    if wa == wb or not same_title(ta["title"], tb["title"]):
        return None
    show = lambda w: ", ".join(sorted(w)) or "no version words"
    return f"title says {show(wa)} vs {show(wb)}"


# ---------------------------------------------------------------- identical or a different version?

def version_differences(ta: dict, tb: dict, sa: Optional[dict], sb: Optional[dict], rules: Rules) -> List[str]:
    """Why two copies whose audio matches are different versions; empty = likely identical."""
    reasons = []
    len_diff = abs(ta["duration"] - tb["duration"])
    if len_diff > rules.identical_max_length_diff:
        reasons.append("length differs by " + (fmt_dur(len_diff) if len_diff >= 60 else
                                               f"{len_diff:.0f} s" if len_diff >= 10 else f"{len_diff:.1f} s"))
    if sa and sb and sa.get("loudness") is not None and sb.get("loudness") is not None:
        d = abs(sa["loudness"] - sb["loudness"])
        if d > rules.identical_max_loudness_diff and d != float("inf"):
            reasons.append(f"loudness differs by {d:.1f} LU (different master or mix)")
    if sa and sb and sa.get("channels") and sb.get("channels") and sa["channels"] != sb["channels"]:
        reasons.append("mono vs stereo")
    if rules.split_on_title_words:
        words = title_word_difference(ta, tb)
        if words:
            reasons.append(words)
    return reasons


def keeper_score(t: dict) -> tuple:
    """Higher is better: lossless, then bitrate, then real tags, then not a '(1)'-style copy, then size."""
    name = os.path.splitext(os.path.basename(t["file"]))[0]
    is_copy = bool(re.search(r"(\(\d\)| \d)$", name))
    meta = sum(bool(v) and not v.startswith("[Unknown") for v in (t["album"], t["artist"]))
    return (t["codec"] in LOSSLESS, round(t["bitrate"], -1), meta, not is_copy, t["size"])


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)


# ---------------------------------------------------------------- broken files

def explain_open_error(err: str) -> str:
    if "RIFF header" in err:
        return "it's a WAV-type file with an MP3-style tag, so the wrong type or damaged"
    if "moov atom not found" in err:
        return "the MP4/M4A index is missing (usually an incomplete download)"
    if "matches no streams" in err:
        return "there's no audio in the file"
    if "Decode error rate" in err:
        return "almost none of the audio decodes (damaged, or encrypted)"
    if "Invalid data found when processing input" in err:
        return "it isn't in a format that can be read (damaged, or not really audio)"
    line = next((l for l in err.splitlines() if l.strip()), err)
    return re.sub(r"\[[^\]]*@ [0-9a-fx]+\] ", "", line)[:120]


def track_problems(t: dict, s: Optional[dict], fp: Optional[tuple], damage: Optional[tuple] = None,
                   check: Optional[tuple] = None) -> List[tuple]:
    """Per-file problems: [(severity, problem, details)]. Uses Plex's loudness analysis (s), our
    fingerprint (fp), the file check of tracks Plex couldn't analyse (check = (problem, details))
    and, if it was run, the full damage check (damage = (errors, first error))."""
    if check:   # the root cause: what Plex measured for a file it can't decode (silence...) adds nothing
        return [(FILECHECK_SEVERITY.get(check[0], "Broken"), check[0], check[1])]
    out = []
    s = s or {}
    peak, loud, lra = s.get("peak"), s.get("loudness"), s.get("lra")
    if peak is not None and peak > BROKEN_PEAK:
        out.append(("Broken", "impossible audio levels",
                    f"Plex measured a peak of {peak:,.0f}x full scale; the file decodes to garbage"))
    elif loud is not None and loud == float("-inf"):
        if t["duration"] < 10 or re.search(INTENTIONAL_SILENCE, t["title"].lower()):
            out.append(("Check", "silent", "Plex measured pure digital silence (probably intentional)"))
        else:
            out.append(("Likely broken", "silent", "Plex measured pure digital silence; a song that "
                                                   "decodes to nothing is usually a damaged file"))
    elif (loud is not None and lra is not None and peak is not None and loud > NOISE_MIN_LOUDNESS
          and lra < NOISE_MAX_LRA and peak >= 0.99):
        out.append(("Check", "constant full-volume noise",
                    f"loudness {loud:.1f} LUFS with almost no variation (noise music, or static)"))
    if fp is not None:
        start, _, err = fp
        if err and ("FileNotFoundError" in err or "No such file" in err):
            out.append(("Check", "missing from disk",
                        "Plex lists this file but it isn't there (moved or deleted outside this tool?)"))
        elif err:
            out.append(("Broken", "couldn't be opened", explain_open_error(err)))
        elif start is not None and t["duration"] > 30:
            heard = len(start) / ITEMS_PER_SECOND
            if heard < 0.5 * min(t["duration"], START_SECONDS):
                out.append(("Broken", "audio ends early",
                            f"Plex says {fmt_dur(t['duration'])}, but the audio stops after about {heard:.0f} s"))
    if damage:
        n, first = damage
        out.append(("Broken" if n >= 10 else "Likely broken", "decoding errors",
                    f"the full damage check found {n} decoding error{'s' if n != 1 else ''}: {first}"))
    return out


def shared_audio_problems(identical_sets, tracks, streams, folder_total) -> Dict[int, tuple]:
    """Sets where one piece of audio sits under 3+ different song titles AND something else points
    to a bad file: a whole album of the same clip, preview-length clips, or audio far too loud
    to be mastered music. Renamed game rips (normal audio, different names) are left out.
    identical_sets: [(group number, [track index, ...])]. Returns {track index: (sev, problem, details)}."""
    out = {}
    for g, members in identical_sets:
        clusters = []
        for k in members:
            for c in clusters:
                if same_title(tracks[c[0]]["title"], tracks[k]["title"]):
                    c.append(k)
                    break
            else:
                clusters.append([k])
        if len(clusters) < 2:
            continue
        reasons = []
        if len(members) >= 3 and all(min(abs(tracks[k]["duration"] - p) for p in PREVIEW_LENGTHS) < 0.25
                                     for k in members):
            reasons.append(f"every copy is exactly {fmt_dur(tracks[members[0]]['duration'])} (preview clips?)")
        loud = (streams.get(tracks[members[0]]["id"]) or {}).get("loudness")
        if len(clusters) >= 3 and loud is not None and loud > NOISE_MIN_LOUDNESS:
            reasons.append(f"and it's {loud:.1f} LUFS, far louder than mastered music")
        folders = defaultdict(int)
        for c in clusters:
            for d in {os.path.dirname(tracks[k]["file"]).lower() for k in c}:
                if folder_total.get(d, 0) <= ALBUM_FOLDER_MAX_TRACKS:
                    folders[d] += 1
        if folders and max(folders.values()) >= 3:
            reasons.append(f"{max(folders.values())} of those titles are in the same album folder")
        if not reasons:
            continue
        details = (f"identical audio under {len(clusters)} different song titles (group {g} in "
                   f"identical copies); " + "; ".join(reasons))
        for k in members:
            out[k] = ("Likely broken", "same audio as differently titled tracks", details)
    return out


# ---------------------------------------------------------------- the whole analysis

@dataclass
class Results:
    tracks: List[dict]                       # every track considered (moved-out files excluded)
    analysed: List[int]                      # indices into tracks with sonic analysis
    identical: List[dict] = field(default_factory=list)
    versions: List[dict] = field(default_factory=list)
    check_by_ear: List[dict] = field(default_factory=list)
    broken: List[dict] = field(default_factory=list)
    not_analysed: List[dict] = field(default_factory=list)   # {"k", "reason", "details"}
    stats: dict = field(default_factory=dict)


def album_key(t: dict):
    """Plex's album, or (for track lists saved before albums were recorded) folder + album name."""
    return t.get("album_id") or (os.path.dirname(t["file"]).lower(), t["album"].lower())


def not_analysed_reasons(tracks: List[dict], checks: Dict[str, tuple]) -> List[dict]:
    """Why each track has no sonic analysis. Plex analyses a whole album at once, and one file it
    can't decode stops the whole album, so a fine track can be blocked by its album-mate."""
    problem_files = defaultdict(list)
    for t in tracks:
        if t["file"] in checks:
            problem_files[album_key(t)].append(t)
    out = []
    for k, t in enumerate(tracks):
        if t.get("analysed"):
            continue
        if t["file"] in checks:
            problem, details = checks[t["file"]]
            out.append({"k": k, "reason": REASON_PROBLEM, "details": f"{problem}: {details}"})
        elif problem_files.get(album_key(t)):
            bad = problem_files[album_key(t)]
            names = ", ".join(f"“{os.path.basename(b['file'])}” ({checks[b['file']][0]})" for b in bad[:3])
            more = f" and {len(bad) - 3} more" if len(bad) > 3 else ""
            out.append({"k": k, "reason": REASON_BLOCKED,
                        "details": f"this file is fine, but Plex analyses whole albums and this album has "
                                   f"{len(bad)} problem file{'s' if len(bad) != 1 else ''}: {names}{more}"})
        elif t.get("plex_analysed"):
            out.append({"k": k, "reason": REASON_DISCARDED,
                        "details": "Plex ran the analysis but threw the result away, which happens mostly with "
                                   "clips under 30 seconds. Nothing to fix"})
        else:
            out.append({"k": k, "reason": REASON_UNKNOWN,
                        "details": "the file looks fine and so does its album; Plex may not have reached it yet "
                                   "(new music waits for the nightly maintenance)"})
    return out


def analyse(tracks: List[dict], pairs: List[Tuple[int, int, float]], fps: Dict[str, tuple],
            streams: Dict[int, dict], rules: Rules, sonic_distance: Callable[[int, int], Optional[float]],
            damage: Optional[Dict[str, tuple]] = None, checks: Optional[Dict[str, tuple]] = None) -> Results:
    """tracks: all tracks. pairs: (track index, track index, sonic distance) close enough to check.
    sonic_distance(i, j): Plex-equivalent distance between two tracks, or None if unknown.
    checks: {file: (problem, details)} from the file check of tracks Plex couldn't analyse."""
    damage, checks = damage or {}, checks or {}
    folder_total = defaultdict(int)
    for t in tracks:
        folder_total[os.path.dirname(t["file"]).lower()] += 1
    res = Results(tracks=tracks, analysed=[k for k, t in enumerate(tracks) if t.get("analysed")])
    res.not_analysed = not_analysed_reasons(tracks, checks)

    problems = {k: track_problems(t, streams.get(t["id"]), fps.get(t["file"]), damage.get(t["file"]),
                                  checks.get(t["file"]))
                for k, t in enumerate(tracks)}
    broken = {k for k, ps in problems.items() if any(p[0] == "Broken" for p in ps)}
    silent = {f for f, v in fps.items() if low_information(v[0])}

    def match(a, b):
        ta, tb = tracks[a], tracks[b]
        return audio_match(fps.get(ta["file"]), fps.get(tb["file"]),
                           abs(ta["duration"] - tb["duration"]) <= SAME_LENGTH_SECONDS)

    uf, linked = UnionFind(), set()
    verdicts = defaultdict(int)
    for a, b, d in pairs:
        ta, tb = tracks[a], tracks[b]
        if ta["file"].lower() == tb["file"].lower():
            continue
        if ta["file"] in silent or tb["file"] in silent:
            verdicts["silence"] += 1
            continue
        m = match(a, b)
        len_diff = abs(ta["duration"] - tb["duration"])
        if m is None:
            ok = d < SONIC_ONLY_MAX_DISTANCE and len_diff <= SONIC_ONLY_MAX_LENGTH_DIFF and same_title(ta["title"], tb["title"])
            kind = "sonic only" if ok else "unconfirmed"
        elif m >= rules.same_audio:
            kind = "same audio"
        elif m >= rules.maybe_audio:
            kind = "check by ear"
            res.check_by_ear.append({"a": a, "b": b, "sonic": d, "match": m})
        else:
            kind = "different audio"
        verdicts[kind] += 1
        if kind in ("same audio", "sonic only"):
            uf.union(a, b)
            linked.update((a, b))

    songs = defaultdict(list)
    for k in linked:
        songs[uf.find(k)].append(k)

    # Within each song: the best copy keeps a set, every copy passing all identical checks against
    # it joins; what's left forms the next version, and so on. Broken copies never keep a set
    # while a healthy copy exists.
    song_versions = []
    for members in songs.values():
        remaining = sorted(members, key=lambda k: (k not in broken, keeper_score(tracks[k])), reverse=True)
        versions = []
        while remaining:
            keeper, rest = remaining[0], []
            copies = [{"k": keeper, "match": None, "sonic_only": False}]
            for k in remaining[1:]:
                tk, t = tracks[keeper], tracks[k]
                m = match(keeper, k)
                reasons = version_differences(tk, t, streams.get(tk["id"]), streams.get(t["id"]), rules)
                if m is None:
                    d = sonic_distance(keeper, k)
                    identical = (not reasons and same_title(tk["title"], t["title"]) and d is not None
                                 and d < SONIC_ONLY_MAX_DISTANCE
                                 and abs(tk["duration"] - t["duration"]) <= SONIC_ONLY_MAX_LENGTH_DIFF)
                else:
                    identical = m >= rules.same_audio and not reasons
                if identical:
                    copies.append({"k": k, "match": m, "sonic_only": m is None})
                else:
                    rest.append(k)
            versions.append(copies)
            remaining = rest
        song_versions.append(versions)

    name_key = lambda k: (tracks[k]["artist"].lower(), tracks[k]["title"].lower())
    sets = [c for vs in song_versions for c in vs if len(c) > 1]
    sets.sort(key=lambda c: (any(x["sonic_only"] for x in c), name_key(c[0]["k"])))
    multi = sorted([vs for vs in song_versions if len(vs) > 1], key=lambda vs: name_key(vs[0][0]["k"]))
    set_no = {id(c): n for n, c in enumerate(sets, start=1)}
    version_group = {id(c): f"V{n}" for n, vs in enumerate(multi, start=1) for c in vs}

    for c in sets:
        keep = tracks[c[0]["k"]]
        note = []
        if not all(same_title(keep["title"], tracks[x["k"]]["title"]) for x in c):
            note.append("titles differ")
        if not rules.split_on_title_words:
            words = {title_word_difference(keep, tracks[x["k"]]) for x in c[1:]} - {None}
            note += sorted(words)
        res.identical.append({
            "group": set_no[id(c)], "copies": c,
            "status": "Sonic match only (couldn't fingerprint)" if any(x["sonic_only"] for x in c) else "Likely identical",
            "note": "; ".join(note), "other_versions": version_group.get(id(c), ""),
        })

    reason_tally = defaultdict(int)
    for vs in multi:
        first = vs[0][0]["k"]
        rows = []
        for n, c in enumerate(vs, start=1):
            k = c[0]["k"]
            if n == 1:
                reasons, m = [], None
            else:
                reasons = version_differences(tracks[first], tracks[k], streams.get(tracks[first]["id"]),
                                              streams.get(tracks[k]["id"]), rules)
                m = match(first, k)
                if not reasons:
                    reasons = ["audio only partly matches version 1"]
                for r in reasons:
                    key = ("version words in title differ" if r.startswith("title says")
                           else r.split(" by ")[0].split(" (")[0])
                    reason_tally[key] += 1
            rows.append({"k": k, "reasons": reasons, "match": m, "identical_copies": len(c) - 1,
                         "identical_group": set_no.get(id(c)) if len(c) > 1 else None})
        res.versions.append({"group": version_group[id(vs[0])], "versions": rows})

    shared = shared_audio_problems([(set_no[id(c)], [x["k"] for x in c]) for c in sets], tracks, streams, folder_total)
    for k, t in enumerate(tracks):
        ps = list(problems[k]) + ([shared[k]] if k in shared else [])
        if ps:
            res.broken.append({"k": k, "problems": sorted(ps, key=lambda p: SEVERITY_ORDER.index(p[0]))})
    folder_flagged = defaultdict(int)
    for b in res.broken:
        folder_flagged[os.path.dirname(tracks[b["k"]]["file"]).lower()] += 1
    for b in res.broken:
        d = os.path.dirname(tracks[b["k"]]["file"]).lower()
        b["folder_flagged"] = f"{folder_flagged[d]} of {folder_total[d]} tracks in this folder"
    res.broken.sort(key=lambda b: (SEVERITY_ORDER.index(b["problems"][0][0]),
                                   os.path.dirname(tracks[b["k"]]["file"]).lower(), tracks[b["k"]]["file"].lower()))
    res.check_by_ear.sort(key=lambda r: -r["match"])

    extra = defaultdict(lambda: [0, 0])
    for g in res.identical:
        for x in g["copies"][1:]:
            bucket = extra["sonic only" if g["status"].startswith("Sonic") else "identical"]
            bucket[0] += 1
            bucket[1] += tracks[x["k"]]["size"]
    problem_tally = defaultdict(int)
    for b in res.broken:
        for p in b["problems"]:
            problem_tally[f"{p[0]}: {p[1]}"] += 1
    res.stats = {
        "tracks": len(tracks), "analysed": len(res.analysed), "not_analysed": len(res.not_analysed),
        "not_analysed_reasons": {r: sum(1 for x in res.not_analysed if x["reason"] == r) for r in NOT_ANALYSED_REASONS},
        "close_pairs": len(pairs), "fingerprinted": len(fps),
        "fingerprint_errors": sum(1 for v in fps.values() if v[2]), "silent_files": len(silent),
        "verdicts": dict(verdicts),
        "identical_groups": sum(1 for g in res.identical if not g["status"].startswith("Sonic")),
        "sonic_only_groups": sum(1 for g in res.identical if g["status"].startswith("Sonic")),
        "extra_copies": extra["identical"][0], "extra_bytes": extra["identical"][1],
        "sonic_only_copies": extra["sonic only"][0],
        "titles_differ_groups": sum(1 for g in res.identical if "titles differ" in g["note"]),
        "version_songs": len(res.versions), "versions": sum(len(v["versions"]) for v in res.versions),
        "version_reasons": dict(reason_tally),
        "broken_files": len(res.broken), "broken_by_severity": {s: sum(1 for b in res.broken if b["problems"][0][0] == s)
                                                               for s in SEVERITY_ORDER},
        "problems": dict(problem_tally),
    }
    return res
