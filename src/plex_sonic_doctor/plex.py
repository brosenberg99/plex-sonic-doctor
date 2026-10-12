"""Talking to the Plex server. Everything here only reads, except request_rescan()."""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Tuple

from .pathmap import PathMapper

Progress = Callable[[str, float], None]   # (message, fraction 0..1 or -1 if unknown)


def _noop(msg, frac=-1):
    pass


def _save_json(data, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def connect(url: str, token: str, timeout: int = 60):
    from plexapi.server import PlexServer
    return PlexServer(url, token, timeout=timeout)


def pin_login(show_code: Callable[[str], None], timeout: int = 600) -> Optional[str]:
    """Sign in with a Plex account the official way: the user enters a short code at
    https://plex.tv/link. No password passes through this program. Returns a token or None."""
    from plexapi.myplex import MyPlexPinLogin
    login = MyPlexPinLogin()
    show_code(login.pin)
    login.run(timeout=timeout)
    login.waitForLogin()
    return login.token


def account_servers(token: str) -> List[dict]:
    """Servers this Plex account can use, with an owner flag."""
    from plexapi.myplex import MyPlexAccount
    account = MyPlexAccount(token=token)
    return [{"name": r.name, "owned": bool(r.owned), "resource": r}
            for r in account.resources() if "server" in (r.provides or "")]


def connect_resource(resource, timeout: int = 20):
    """Connect to an account server, trying its local addresses first. Returns (server, url, token)."""
    server = resource.connect(timeout=timeout)
    return server, server._baseurl, resource.accessToken or server._token


def music_sections(server) -> List[Tuple[str, str, List[str]]]:
    """(key, title, folders) for every music library."""
    return [(str(s.key), s.title, list(s.locations)) for s in server.library.sections() if s.type == "artist"]


def fetch_tracks(server, section_keys: List[str], mapper: PathMapper, progress: Progress = _noop) -> List[dict]:
    """Every track in the chosen music libraries, in pages of 5,000."""
    tracks = []
    for key, title, _ in music_sections(server):
        if section_keys and key not in section_keys:
            continue
        start, page = 0, 5000
        while True:
            el = server.query(f"/library/sections/{key}/all?type=10"
                              f"&X-Plex-Container-Start={start}&X-Plex-Container-Size={page}")
            items = list(el)
            for t in items:
                media = t.find("Media")
                part = media.find("Part") if media is not None else None
                if part is None or not part.get("file"):
                    continue
                tracks.append({
                    "id": int(t.get("ratingKey")),
                    "section": title,
                    "title": t.get("title", ""),
                    "artist": t.get("originalTitle") or t.get("grandparentTitle", ""),
                    "album": t.get("parentTitle", ""),
                    "duration": int(t.get("duration") or media.get("duration") or 0) / 1000,
                    "codec": (media.get("audioCodec") or part.get("container") or "").lower(),
                    "bitrate": int(media.get("bitrate") or 0),
                    "size": int(part.get("size") or 0),
                    "plex_file": part.get("file"),
                    "file": mapper.to_local(part.get("file")),
                    "updated": t.get("updatedAt", ""),
                    "analysed": t.get("musicAnalysisVersion") is not None,
                })
            start += len(items)
            total = int(el.get("totalSize", start))
            progress(f"Reading the track list from '{title}': {start:,} of {total:,}", start / max(total, 1))
            if not items or start >= total:
                break
    return tracks


def fetch_streams(server, tracks: List[dict], cache_path: str, workers: int = 8,
                  progress: Progress = _noop) -> Dict[int, dict]:
    """Loudness, loudness range, peak and channel count per track, from Plex's own loudness
    analysis. One request per track, so results are cached and only re-fetched when Plex
    reports the track changed."""
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    need = [t for t in tracks if str(t["id"]) not in cache
            or (t.get("updated") and cache[str(t["id"])].get("updated") != t["updated"])]
    if need:
        def one(t):
            try:
                el = server.query(f"/library/metadata/{t['id']}")
            except Exception:
                return t, None
            s = next((s for s in el.iter("Stream") if s.get("streamType") == "2"), None)
            num = lambda k: float(s.get(k)) if s is not None and s.get(k) not in (None, "") else None
            return t, {"updated": t.get("updated", ""), "loudness": num("loudness"), "lra": num("lra"),
                       "peak": num("peak"), "channels": num("channels")}

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for t, v in pool.map(one, need):
                if v is not None:
                    cache[str(t["id"])] = v
                done += 1
                if done % 500 == 0 or done == len(need):
                    progress(f"Reading loudness details from Plex: {done:,} of {len(need):,}", done / len(need))
        _save_json(cache, cache_path)
    return {int(k): v for k, v in cache.items()}


def nearest(server, track_id: int, limit: int = 50, max_distance: float = 0.25) -> List[Tuple[int, float]]:
    """Plex's own "sonically similar" list for one track: [(track id, distance)]."""
    el = server.query(f"/library/metadata/{track_id}/nearest?limit={limit}&maxDistance={max_distance}")
    return [(int(t.get("ratingKey")), float(t.get("distance"))) for t in el if t.get("distance") is not None]


def nearest_pairs(server, track_ids: List[int], cache_path: str, max_distance: float, workers: int = 8,
                  progress: Progress = _noop) -> Dict[Tuple[int, int], float]:
    """Close pairs straight from Plex, for when its analysis files can't be reached from this
    computer. Slower (one request per track) but needs nothing but the network. Cached."""
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    need = [i for i in track_ids if str(i) not in cache]

    def one(i):
        try:
            return i, nearest(server, i, limit=50, max_distance=max_distance)
        except Exception:
            return i, None

    done = 0
    if need:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, got in pool.map(one, need):
                if got is not None:
                    cache[str(i)] = got
                done += 1
                if done % 500 == 0 or done == len(need):
                    progress(f"Asking Plex for sonically similar tracks: {done:,} of {len(need):,}", done / len(need))
                    _save_json(cache, cache_path)
    pairs = {}
    wanted = set(track_ids)
    for i in track_ids:
        for j, d in cache.get(str(i), []):
            if j != i and j in wanted and d < max_distance:
                key = (min(i, j), max(i, j))
                pairs[key] = min(d, pairs.get(key, d))
    return pairs


def request_rescan(server, section_keys: List[str]) -> None:
    """Ask Plex to rescan the music libraries (after moving files out or back)."""
    for key, _, _ in music_sections(server):
        if not section_keys or key in section_keys:
            server.library.sectionByID(int(key)).update()
