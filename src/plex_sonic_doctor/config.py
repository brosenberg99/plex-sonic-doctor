"""Where the app keeps its data, and the user's settings.

Everything personal (Plex token, caches, reports, move logs) lives in the user's own
app-data folder, never next to the code:

    Windows  %LOCALAPPDATA%\\PlexSonicDoctor
    macOS    ~/Library/Application Support/PlexSonicDoctor
    Linux    $XDG_DATA_HOME/plex-sonic-doctor  (default ~/.local/share/plex-sonic-doctor)

Set SONICDOCTOR_HOME to use another folder.
"""
import dataclasses
import json
import os
import sys
from dataclasses import dataclass, field
from typing import List


def data_dir() -> str:
    override = os.environ.get("SONICDOCTOR_HOME")
    if override:
        path = override
    elif sys.platform == "win32":
        path = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "PlexSonicDoctor")
    elif sys.platform == "darwin":
        path = os.path.expanduser("~/Library/Application Support/PlexSonicDoctor")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        path = os.path.join(base, "plex-sonic-doctor")
    os.makedirs(path, exist_ok=True)
    return path


def sub_dir(name: str) -> str:
    path = os.path.join(data_dir(), name)
    os.makedirs(path, exist_ok=True)
    return path


@dataclass
class PathMap:
    plex: str    # how the Plex server writes the folder, e.g. "E:\\" or "/data/music"
    local: str   # the same folder as seen from this computer, e.g. "H:\\" or "/Volumes/music"


@dataclass
class Rules:
    """What counts as an identical copy; the rest are different versions. Tuned on a 109k-track library."""
    identical_max_length_diff: float = 2.0     # seconds
    identical_max_loudness_diff: float = 0.5   # LU; one master re-encoded stays within ~0.4
    split_on_title_words: bool = True          # "Song (Live)" vs "Song" -> different versions
    same_audio: float = 0.85                   # fingerprint agreement for "same audio"
    maybe_audio: float = 0.65                  # below same_audio but above this -> check by ear


@dataclass
class Config:
    plex_url: str = ""
    plex_token: str = ""
    server_name: str = ""
    sections: List[str] = field(default_factory=list)   # music library keys; empty = all music libraries
    sonic_dir: str = ""          # Plex's "Music Analysis N" folder as seen from here; "" = use the Plex API instead
    path_maps: List[PathMap] = field(default_factory=list)
    holding_dir: str = ""        # where moved copies go; "" = <drive or volume>/_duplicates_holding
    update_playlists: bool = False   # put the kept copy in a moved copy's place in playlists/collections
    ffmpeg: str = "ffmpeg"
    fpcalc: str = "fpcalc"
    plex_scale: float = 1.08     # Plex distance = scale x unit-vector distance; re-measured at setup
    scale_measured: bool = False
    workers: int = 8
    acoustid_key: str = ""       # set = allowed to look tracks up in AcoustID (sends fingerprints, nothing else)
    picard: str = ""             # MusicBrainz Picard's program; "" = find it
    rules: Rules = field(default_factory=Rules)

    @property
    def configured(self) -> bool:
        return bool(self.plex_url and self.plex_token)


def config_path() -> str:
    return os.path.join(data_dir(), "config.json")


def load() -> Config:
    path = config_path()
    if not os.path.exists(path):
        return Config()
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    rules = Rules(**{k: v for k, v in raw.pop("rules", {}).items() if k in Rules.__dataclass_fields__})
    maps = [PathMap(**m) for m in raw.pop("path_maps", [])]
    known = {k: v for k, v in raw.items() if k in Config.__dataclass_fields__}
    return Config(**known, path_maps=maps, rules=rules)


def save(cfg: Config) -> None:
    path = config_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dataclasses.asdict(cfg), f, indent=2)
    os.replace(tmp, path)
    if sys.platform != "win32":
        os.chmod(path, 0o600)   # it holds the Plex token and the AcoustID key


def redacted(cfg: Config) -> dict:
    """Settings safe to show or log (no token)."""
    d = dataclasses.asdict(cfg)
    d["plex_token"] = "(set)" if cfg.plex_token else ""
    d["acoustid_key"] = "(set)" if cfg.acoustid_key else ""
    return d
