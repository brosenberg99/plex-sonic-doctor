"""First-time setup, step by step. Each step is a plain function so the GUI can reuse it;
run() strings them together for the command line."""
import os
import sys
from typing import Callable, List, Optional

from . import config, fingerprint, mover, plex, plexdata, scan
from .config import Config, PathMap
from .pathmap import PathMapper, share_found, suggest_map


def sample_plex_paths(server, section_keys: List[str], n: int = 30) -> List[str]:
    """A few track paths as the server writes them, spread over the chosen libraries."""
    out = []
    for key, _, _ in plex.music_sections(server):
        if section_keys and key not in section_keys:
            continue
        el = server.query(f"/library/sections/{key}/all?type=10&X-Plex-Container-Start=0&X-Plex-Container-Size={n}")
        for t in el:
            part = t.find("Media/Part")
            if part is not None and part.get("file"):
                out.append(part.get("file"))
    return out


def check_sonic_dir(path: str, server=None) -> Optional[str]:
    """The "Music Analysis N" folder found at or under path, or None."""
    return plexdata.find_music_analysis(path) if path and os.path.isdir(path) else None


def tools_status(cfg: Config) -> str:
    backend, why = fingerprint.detect(cfg.ffmpeg, cfg.fpcalc)
    return why if backend else f"{why}. {fingerprint.install_help()}"


# ---------------------------------------------------------------- command-line wizard

def _ask(prompt: str, default: str = "") -> str:
    shown = f" [{default}]" if default else ""
    answer = input(f"{prompt}{shown}: ").strip()
    return answer or default


def _choose(prompt: str, options: List[str], default: int = 1) -> int:
    for n, o in enumerate(options, start=1):
        print(f"  {n}. {o}")
    while True:
        a = _ask(prompt, str(default))
        if a.isdigit() and 1 <= int(a) <= len(options):
            return int(a) - 1
        print("  Please type one of the numbers.")


def run(cfg: Config) -> Config:
    print("\nPlex Sonic Doctor setup. Nothing in your library is changed by setup.\n")

    # 1. Sign in and pick the server
    how = _choose("How do you want to connect to Plex?",
                  ["Sign in with my Plex account (opens plex.tv/link with a code) - easiest",
                   "Type the server address and a Plex token myself"])
    if how == 0:
        token = plex.pin_login(lambda code: print(f"\n  Go to https://plex.tv/link and enter this code:  {code}\n"
                                                  f"  (waiting up to 10 minutes...)"))
        if not token:
            sys.exit("Sign-in didn't complete. Run setup again.")
        servers = plex.account_servers(token)
        if not servers:
            sys.exit("This Plex account has no servers.")
        pick = 0 if len(servers) == 1 else _choose("Which server?", [s["name"] + ("" if s["owned"] else " (shared with you)")
                                                                       for s in servers])
        print("  Connecting...")
        server, url, server_token = plex.connect_resource(servers[pick]["resource"])
        cfg.plex_url, cfg.plex_token, cfg.server_name = url, server_token, servers[pick]["name"]
    else:
        cfg.plex_url = _ask("Server address", cfg.plex_url or "http://localhost:32400")
        cfg.plex_token = _ask("Plex token (see https://support.plex.tv/articles/204059436)", cfg.plex_token)
        server = plex.connect(cfg.plex_url, cfg.plex_token)
        cfg.server_name = server.friendlyName
    print(f"  Connected to {cfg.server_name}.")

    # 2. Music libraries
    sections = plex.music_sections(server)
    if not sections:
        sys.exit("This server has no music libraries.")
    if len(sections) == 1:
        cfg.sections = [sections[0][0]]
    else:
        print("\nMusic libraries:")
        for n, (k, title, folders) in enumerate(sections, start=1):
            print(f"  {n}. {title}  ({', '.join(folders)})")
        a = _ask("Which ones? Numbers separated by commas, or Enter for all", "")
        cfg.sections = [sections[int(x) - 1][0] for x in a.replace(" ", "").split(",") if x.isdigit()] if a else []
    config.save(cfg)

    # 3. Can this computer open the music files?
    samples = sample_plex_paths(server, cfg.sections)
    mapper = PathMapper(cfg.path_maps)
    found = share_found(mapper, samples)
    while found < 0.8:
        print(f"\nThis computer can't open the music files yet (found {found:.0%} of a sample).")
        print(f"  Plex sees them like this: {samples[0] if samples else '(no tracks)'}")
        local = _ask("Where is your music folder on THIS computer? (e.g. H:\\ or /Volumes/Music; Enter to skip)", "")
        if not local:
            print("  Skipping. Without file access the audio can't be fingerprinted, so results are less certain.")
            break
        m = next((suggest_map(p, local) for p in samples if suggest_map(p, local)), None)
        if not m:
            print("  None of the sample tracks are in that folder. Try the folder that holds your artist folders, or the drive.")
            continue
        cfg.path_maps = [x for x in cfg.path_maps if x.plex != m.plex] + [m]
        mapper = PathMapper(cfg.path_maps)
        found = share_found(mapper, samples)
        print(f"  Plex's {m.plex} = {m.local} here; {found:.0%} of the sample found.")
    config.save(cfg)

    # 4. Plex's sonic analysis files
    hit = plexdata.auto_find()
    if hit:
        print(f"\nFound Plex's sonic analysis on this computer: {hit}")
        cfg.sonic_dir = hit
    else:
        print("\nPlex's sonic analysis files are in its data folder, under Plug-in Support/Databases/Music Analysis N.")
        print("  If the server runs on another machine, share that folder (read access is enough) and enter it here.")
        print("  See https://support.plex.tv/articles/202915258 for where it lives on each system.")
        while True:
            p = _ask("Plex data folder (Enter to use the slower over-the-network method instead)", cfg.sonic_dir)
            if not p:
                cfg.sonic_dir = ""
                print("  OK: Plex will be asked for each track's similar tracks over the network (slower, ~30 min for 100k tracks).")
                break
            hit = check_sonic_dir(p)
            if hit:
                cfg.sonic_dir = hit
                print(f"  Found {hit}")
                break
            print("  No 'Music Analysis' folder with Track.tree and Mapping.db there.")
    config.save(cfg)

    # 5. Fingerprint tools
    print("\nAudio fingerprints: " + tools_status(cfg))

    # 6. Measure Plex's distance scale on this library
    if cfg.sonic_dir:
        print("\nMeasuring Plex's distance scale on your library (about 20 requests)...")
        k = scan.measure_scale(cfg)
        if k:
            cfg.plex_scale, cfg.scale_measured = round(k, 4), True
            print(f"  Scale {k:.3f} (1.08 was measured on another library).")
        else:
            print("  Couldn't measure it; using the default 1.08.")
    config.save(cfg)

    # 7. Holding folder
    example = mapper.to_local(samples[0]) if samples else ""
    default = mover.holding_root_for(example) if example else ""
    print(f"\nCopies you choose to remove are moved (not deleted) to a holding folder: {cfg.holding_dir or default}")
    other = _ask("Press Enter to keep that, or type another folder", "")
    if other:
        cfg.holding_dir = other
    config.save(cfg)
    print(f"\nSetup saved to {config.config_path()}.\nNext: run  sonicdoctor scan")
    return cfg
