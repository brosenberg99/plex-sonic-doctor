"""Translate file paths as the Plex server writes them into paths this computer can open.

The server and this computer often see the same music under different names:
"E:\\Music\\..." on the server is "H:\\Music\\..." here, or a Docker container's
"/data/music/..." is "\\\\nas\\music\\..." on a laptop. A user only needs to pick their
music folder; suggest_map() works out the rest from one real track path.
"""
import os
import re
from typing import Callable, Iterable, List, Optional

from .config import PathMap


def _is_windows_style(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:", path)) or path.startswith("\\\\")


def _parts(path: str) -> List[str]:
    return [p for p in re.split(r"[\\/]+", path)]


def _join(root: str, rest: List[str]) -> str:
    sep = "\\" if _is_windows_style(root) else "/"
    root = root.rstrip("\\/")
    tail = sep.join(p for p in rest if p)
    if not root:                       # POSIX root "/"
        return sep + tail
    return root + sep + tail if tail else root + sep


class PathMapper:
    def __init__(self, maps: Iterable[PathMap] = ()):
        # Longest server prefix first, so "/data/music/live" wins over "/data/music".
        self.maps = sorted(maps, key=lambda m: len(m.plex), reverse=True)

    def to_local(self, plex_path: str) -> str:
        for m in self.maps:
            prefix = m.plex.rstrip("\\/")
            if _is_windows_style(prefix):
                matches = plex_path.lower().startswith(prefix.lower())
            else:
                matches = plex_path.startswith(prefix)
            if matches and (len(plex_path) == len(prefix) or plex_path[len(prefix)] in "\\/"):
                return _join(m.local, _parts(plex_path[len(prefix):]))
        return plex_path


def suggest_map(plex_path: str, local_root: str,
                exists: Callable[[str], bool] = os.path.exists) -> Optional[PathMap]:
    """Find the server prefix that, swapped for local_root, turns plex_path into a file that
    exists here. Tries the longest remaining tail first: with "E:\\Music\\A\\b.mp3" and a
    local root of "H:\\" it finds "H:\\Music\\A\\b.mp3", so the map is E:\\ -> H:\\."""
    parts = _parts(plex_path)
    for i in range(1, len(parts)):
        candidate = _join(local_root, parts[i:])
        if exists(candidate):
            prefix_parts = parts[:i]
            if _is_windows_style(plex_path):
                prefix = "\\".join(prefix_parts) + "\\"
            else:
                prefix = "/".join(prefix_parts) + "/"
                if not prefix.startswith("/"):
                    prefix = "/" + prefix.lstrip("/")
            return PathMap(plex=prefix, local=local_root)
    return None


def share_found(mapper: PathMapper, plex_paths: List[str], exists: Callable[[str], bool] = os.path.exists) -> float:
    """Fraction of sample tracks that can be opened from here."""
    if not plex_paths:
        return 0.0
    return sum(1 for p in plex_paths if exists(mapper.to_local(p))) / len(plex_paths)
