"""Write the results: CSV files for spreadsheets, results.json for the GUI, and a summary."""
import csv
import json
import os
import time
from typing import Dict

from .analysis import NOT_ANALYSED_REASONS, Results, SEVERITY_ORDER, fmt_dur

FILES = {
    "identical": "likely_identical.csv",
    "versions": "different_versions.csv",
    "broken": "possibly_broken.csv",
    "check": "check_by_ear.csv",
    "not_analysed": "not_analysed.csv",
}


def _loud(s):
    v = (s or {}).get("loudness")
    return "" if v is None else ("silent" if v == float("-inf") else f"{v:.1f}")


def _channels(s):
    c = (s or {}).get("channels")
    return "" if not c else "mono" if c == 1 else "stereo" if c == 2 else f"{c:.0f} ch"


def _pct(m):
    return "" if m is None else f"{100 * m:.0f}%"


def _writer(path):
    f = open(path, "w", newline="", encoding="utf-8-sig")   # BOM: Excel then reads non-English names right
    return f, csv.writer(f)


def write(res: Results, streams: Dict[int, dict], out_dir: str) -> Dict[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    T = res.tracks
    paths = {k: os.path.join(out_dir, v) for k, v in FILES.items()}

    f, w = _writer(paths["identical"])
    w.writerow(["group", "copies", "status", "note", "suggestion", "title", "artist", "album", "source", "length", "format",
                "kbps", "size_mb", "loudness_lu", "channels", "audio_match_to_keep", "other_versions", "file",
                "plex_id", "decision"])
    for g in res.identical:
        for n, c in enumerate(g["copies"]):
            t, s = T[c["k"]], streams.get(T[c["k"]]["id"])
            w.writerow([g["group"], len(g["copies"]), g["status"], g["note"], "KEEP" if n == 0 else "remove?",
                        t["title"], t["artist"], t["album"], t.get("source", ""), fmt_dur(t["duration"]), t["codec"],
                        t["bitrate"], f"{t['size'] / 1e6:.1f}", _loud(s), _channels(s),
                        "" if n == 0 else (_pct(c["match"]) if c["match"] is not None else "sonic only"),
                        f"see {g['other_versions']} in {FILES['versions']}" if g["other_versions"] else "",
                        t["file"], t["id"], ""])
    f.close()

    f, w = _writer(paths["versions"])
    w.writerow(["group", "versions", "version", "title", "artist", "album", "source", "length", "format", "kbps", "size_mb",
                "loudness_lu", "channels", "how_it_differs_from_version_1", "audio_match_to_version_1",
                "identical_copies", "file", "plex_id", "decision"])
    for song in res.versions:
        for n, v in enumerate(song["versions"], start=1):
            t, s = T[v["k"]], streams.get(T[v["k"]]["id"])
            same = (f"{v['identical_copies']} (group {v['identical_group']} in {FILES['identical']})"
                    if v["identical_copies"] else "0")
            w.writerow([song["group"], len(song["versions"]), n, t["title"], t["artist"], t["album"], t.get("source", ""),
                        fmt_dur(t["duration"]), t["codec"], t["bitrate"], f"{t['size'] / 1e6:.1f}", _loud(s),
                        _channels(s), "; ".join(v["reasons"]), _pct(v["match"]), same, t["file"], t["id"], ""])
    f.close()

    f, w = _writer(paths["broken"])
    w.writerow(["severity", "problem", "details", "folder_flagged", "title", "artist", "album", "source", "length", "format",
                "kbps", "loudness_lu", "peak", "file", "plex_id", "notes"])
    for b in res.broken:
        t, s = T[b["k"]], streams.get(T[b["k"]]["id"]) or {}
        ps = b["problems"]
        w.writerow([ps[0][0], "; ".join(p[1] for p in ps), "; ".join(p[2] for p in ps), b["folder_flagged"],
                    t["title"], t["artist"], t["album"], t.get("source", ""), fmt_dur(t["duration"]), t["codec"],
                    t["bitrate"], _loud(s), "" if s.get("peak") is None else f"{s['peak']:.2f}", t["file"], t["id"], ""])
    f.close()

    f, w = _writer(paths["check"])
    w.writerow(["sonic_distance", "audio_match", "title1", "artist1", "album1", "length1", "file1",
                "title2", "artist2", "album2", "length2", "file2", "decision"])
    for r in res.check_by_ear:
        a, b = T[r["a"]], T[r["b"]]
        w.writerow([f"{r['sonic']:.4f}", _pct(r["match"]), a["title"], a["artist"], a["album"], fmt_dur(a["duration"]),
                    a["file"], b["title"], b["artist"], b["album"], fmt_dur(b["duration"]), b["file"], ""])
    f.close()

    f, w = _writer(paths["not_analysed"])
    w.writerow(["reason", "details", "title", "artist", "album", "source", "length", "format", "file", "plex_id"])
    order = {r: n for n, r in enumerate(NOT_ANALYSED_REASONS)}
    for r in sorted(res.not_analysed, key=lambda r: (order.get(r["reason"], 9), T[r["k"]]["file"].lower())):
        t = T[r["k"]]
        w.writerow([r["reason"], r["details"], t["title"], t["artist"], t["album"], t.get("source", ""),
                    fmt_dur(t["duration"]), t["codec"],
                    t["file"], t["id"]])
    f.close()

    summary = summary_text(res)
    with open(os.path.join(out_dir, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(summary)
    _write_json(res, streams, os.path.join(out_dir, "results.json"))
    return paths


def summary_text(res: Results) -> str:
    s = res.stats
    v = s["verdicts"]
    lines = [
        f"Plex Sonic Doctor scan, {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"Tracks: {s['tracks']:,}  (with sonic analysis: {s['analysed']:,}, without: {s['not_analysed']:,})",
    ]
    lines += [f"  without, {reason}: {n:,}" for reason, n in s.get("not_analysed_reasons", {}).items() if n]
    lines += [
        f"Close sonic pairs checked: {s['close_pairs']:,};  files fingerprinted: {s['fingerprinted']:,}",
        f"  same audio {v.get('same audio', 0):,}, partly matching {v.get('check by ear', 0):,}, "
        f"different audio {v.get('different audio', 0):,}, silence left out {v.get('silence', 0):,}",
        "",
        f"Likely identical: {s['identical_groups']:,} groups, {s['extra_copies']:,} extra copies, "
        f"{s['extra_bytes'] / 1e9:.1f} GB  (+{s['sonic_only_groups']} sonic-only groups)",
        f"  groups whose titles differ (worth a closer look): {s['titles_differ_groups']:,}",
        f"Different versions: {s['version_songs']:,} songs, {s['versions']:,} versions",
    ]
    lines += [f"  {k}: {n:,}" for k, n in sorted(s["version_reasons"].items(), key=lambda x: -x[1])]
    lines += ["", f"Possibly broken: {s['broken_files']:,} files"]
    for sev in SEVERITY_ORDER:
        lines.append(f"  {sev}: {s['broken_by_severity'].get(sev, 0):,}")
        lines += [f"    {k.split(': ', 1)[1]}: {n:,}" for k, n in sorted(s["problems"].items(), key=lambda x: -x[1])
                  if k.startswith(sev + ":")]
    lines += ["", "Nothing was deleted, moved or changed."]
    return "\n".join(lines)


def _write_json(res: Results, streams, path):
    """Everything the GUI needs, keyed by track index into 'tracks'."""
    def track(t):
        s = streams.get(t["id"]) or {}
        loud = s.get("loudness")
        return {**{k: t[k] for k in ("id", "title", "artist", "album", "duration", "codec", "bitrate", "size", "file")},
                **{k: t.get(k) for k in ("album_artist", "year", "track", "disc", "added", "root", "source")},
                "loudness": None if loud is None or loud == float("-inf") else loud, "silent": loud == float("-inf"),
                "peak": s.get("peak"), "channels": s.get("channels")}
    data = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tracks": [track(t) for t in res.tracks],
        "identical": res.identical, "versions": res.versions, "broken": res.broken,
        "check_by_ear": res.check_by_ear, "not_analysed": res.not_analysed, "stats": res.stats,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)
