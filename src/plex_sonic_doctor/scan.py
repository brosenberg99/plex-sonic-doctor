"""One full scan: Plex track list -> close sonic pairs -> fingerprints -> loudness -> analysis -> reports."""
import json
import os
from typing import Callable, Optional

import numpy as np

from . import analysis, config, filecheck, fingerprint, mover, plex, plexdata, reports, sources
from .pathmap import PathMapper

CANDIDATE_MAX_DISTANCE = 0.06   # Plex sonic distance at which a pair is worth fingerprinting


class Stopped(Exception):
    pass


def run_scan(cfg: config.Config, progress: Callable = lambda m, f=-1: None, refresh: bool = True,
             should_stop: Callable[[], bool] = lambda: False):
    """Returns (Results, {report name: path}). refresh=False reuses the last track list and
    analysis files (quick re-run after changing the rules)."""
    cache = config.sub_dir("cache")
    tracks_path = os.path.join(cache, "tracks.json")
    sonic_copy = os.path.join(cache, "Music Analysis")
    server = None

    def get_server():
        nonlocal server
        if server is None:
            progress("Connecting to Plex", -1)
            server = plex.connect(cfg.plex_url, cfg.plex_token)
        return server

    def check_stop():
        if should_stop():
            raise Stopped()

    backend, why = fingerprint.detect(cfg.ffmpeg, cfg.fpcalc)
    progress(f"Fingerprints: {why}", -1)

    if refresh or not os.path.exists(tracks_path):
        tracks = plex.fetch_tracks(get_server(), cfg.sections, PathMapper(cfg.path_maps), progress)
        with open(tracks_path, "w", encoding="utf-8") as f:
            json.dump(tracks, f)
        if cfg.sonic_dir:
            progress("Copying Plex's sonic analysis files", -1)
            plexdata.copy_sonic_files(cfg.sonic_dir, sonic_copy)
    else:
        with open(tracks_path, encoding="utf-8") as f:
            tracks = json.load(f)
    check_stop()

    # Files this tool moved to the holding folder are gone, even before Plex rescans.
    held = mover.moved_files(config.sub_dir("moves"))
    tracks = [t for t in tracks if t["file"] not in held]
    if held:
        progress(f"Leaving out {len(held):,} files that are in the holding folder", -1)

    if cfg.sonic_dir and os.path.exists(os.path.join(sonic_copy, "Track.tree")):
        profiles, slot_of = plexdata.load_profiles(sonic_copy)
        for t in tracks:
            # Plex can mark a track analysed yet keep no sound profile for it (it throws away
            # results it can't use), so the profiles decide; Plex's own flag is kept to tell why.
            t["plex_analysed"] = t["analysed"]
            t["analysed"] = t["id"] in slot_of
        idx = [k for k, t in enumerate(tracks) if t["analysed"]]
        units = plexdata.unit_vectors(profiles, [slot_of[tracks[k]["id"]] for k in idx])
        progress(f"Comparing {len(idx):,} sound profiles with each other", -1)
        pi, pj, pd = plexdata.candidate_pairs(units, cfg.plex_scale, CANDIDATE_MAX_DISTANCE)
        pairs = [(idx[a], idx[b], float(d)) for a, b, d in zip(pi.tolist(), pj.tolist(), pd.tolist())]
        pos = {k: n for n, k in enumerate(idx)}

        def sonic_distance(a, b):
            if a in pos and b in pos:
                return float(cfg.plex_scale * np.linalg.norm(units[pos[a]] - units[pos[b]]))
            return None
    else:
        # No access to the analysis files from here: ask Plex for each track's similar tracks.
        idx = [k for k, t in enumerate(tracks) if t.get("analysed")]
        id_to_k = {tracks[k]["id"]: k for k in idx}
        found = plex.nearest_pairs(get_server(), [tracks[k]["id"] for k in idx], os.path.join(cache, "nearest.json"),
                                   CANDIDATE_MAX_DISTANCE, cfg.workers, progress)
        pairs = [(id_to_k[a], id_to_k[b], d) for (a, b), d in found.items() if a in id_to_k and b in id_to_k]
        by_ids = {(min(a, b), max(a, b)): d for (a, b), d in found.items()}

        def sonic_distance(a, b):
            ia, ib = tracks[a]["id"], tracks[b]["id"]
            return by_ids.get((min(ia, ib), max(ia, ib)))
    progress(f"{len(pairs):,} close pairs to check", -1)
    check_stop()

    involved = sorted({k for a, b, _ in pairs for k in (a, b)})
    conn = fingerprint.open_cache(os.path.join(cache, "fingerprints.sqlite3"))
    try:
        # A quick re-sort uses the saved fingerprints as they are: checking 30,000 files over a
        # network share for changes takes minutes, and a full scan does that anyway.
        if backend and refresh:
            fingerprint.ensure_fingerprints(conn, backend, [tracks[k] for k in involved], cfg.workers,
                                            progress, should_stop)
        check_stop()
        fps = fingerprint.load_fingerprints(conn, {tracks[k]["file"] for k in involved})
        damage = fingerprint.load_damage(conn)
        if refresh:
            filecheck.run_file_checks(conn, cfg.ffmpeg, [t for t in tracks if not t["analysed"]],
                                      max(1, cfg.workers // 2), progress, should_stop)
            check_stop()
        checks = filecheck.load_file_checks(conn)
    finally:
        conn.close()

    streams = plex.fetch_streams(_LazyServer(get_server), tracks, os.path.join(cache, "streams.json"),
                                 cfg.workers, progress)
    check_stop()
    progress("Sorting copies into identical, versions and broken", -1)
    not_analysed = {t["file"] for t in tracks if not t["analysed"]}
    checks = {f: c for f, c in checks.items() if f in not_analysed}
    res = analysis.analyse(tracks, pairs, fps, streams, cfg.rules, sonic_distance, damage, checks)
    res.stats["fingerprint_method"] = why
    check_stop()
    # Where each listed file came from (iTunes Store, Bandcamp...): only the tracks in the lists.
    listed = {tracks[k]["file"] for k in listed_tracks(res)}
    conn = fingerprint.open_cache(os.path.join(cache, "fingerprints.sqlite3"))
    try:
        sources.ensure_sources(conn, listed, cfg.workers, refresh, progress, should_stop)
        found = sources.load_sources(conn, listed)
    finally:
        conn.close()
    for t in tracks:
        t["source"] = found.get(t["file"], "")
    paths = reports.write(res, streams, config.sub_dir("reports"))
    progress("Done", 1)
    return res, paths


def listed_tracks(res) -> set:
    """Every track shown in any of the reports."""
    ks = {c["k"] for g in res.identical for c in g["copies"]}
    ks |= {v["k"] for s in res.versions for v in s["versions"]}
    ks |= {b["k"] for b in res.broken}
    ks |= {k for r in res.check_by_ear for k in (r["a"], r["b"])}
    ks |= {r["k"] if isinstance(r, dict) else r for r in res.not_analysed}
    return ks


class _LazyServer:
    """Connects only when a request is actually made, so a quick re-run with everything
    cached works even while the server is unreachable."""
    def __init__(self, get_server):
        self._get = get_server

    def query(self, *a, **k):
        return self._get().query(*a, **k)


def run_damage_check(cfg: config.Config, folder_filter: str = "", progress=lambda m, f=-1: None,
                     should_stop=lambda: False) -> int:
    """Decode every file in the last track list (or those whose path contains folder_filter)."""
    tracks_path = os.path.join(config.sub_dir("cache"), "tracks.json")
    if not os.path.exists(tracks_path):
        raise RuntimeError("Run a scan first, so the track list is known.")
    with open(tracks_path, encoding="utf-8") as f:
        tracks = json.load(f)
    if folder_filter:
        tracks = [t for t in tracks if folder_filter.lower() in t["file"].lower()]
    conn = fingerprint.open_cache(os.path.join(config.sub_dir("cache"), "fingerprints.sqlite3"))
    try:
        return fingerprint.run_damage_check(conn, cfg.ffmpeg, tracks, max(1, cfg.workers // 2), progress, should_stop)
    finally:
        conn.close()


def measure_scale(cfg: config.Config, seeds: int = 20, progress=lambda m, f=-1: None) -> Optional[float]:
    """Compare our distances with Plex's own "sonically similar" answers and fit the factor."""
    if not cfg.sonic_dir:
        return None
    sonic_copy = os.path.join(config.sub_dir("cache"), "Music Analysis")
    plexdata.copy_sonic_files(cfg.sonic_dir, sonic_copy)
    profiles, slot_of = plexdata.load_profiles(sonic_copy)
    server = plex.connect(cfg.plex_url, cfg.plex_token)
    rng = np.random.default_rng(7)
    ids = list(slot_of)
    a_slots, b_slots, dists = [], [], []
    for n, seed in enumerate(rng.choice(ids, size=min(seeds, len(ids)), replace=False).tolist(), start=1):
        progress(f"Measuring Plex's distance scale: {n} of {seeds}", n / seeds)
        try:
            for other, d in plex.nearest(server, seed, limit=50, max_distance=0.5):
                if other in slot_of:
                    a_slots.append(slot_of[seed]); b_slots.append(slot_of[other]); dists.append(d)
        except Exception:
            continue
    if len(dists) < 50:
        return None
    ua = plexdata.unit_vectors(profiles, a_slots)
    ub = plexdata.unit_vectors(profiles, b_slots)
    return plexdata.fit_scale(ua, ub, np.array(dists))
