"""Command line:  sonicdoctor <command>   (run  sonicdoctor -h  for the list)"""
import argparse
import os
import sys
import time

from . import __version__, acoustid, config, fingerprint, mover, plex, plexlists, scan, setup_wizard
from .pathmap import PathMapper


def _progress(msg, frac=-1):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def _need_setup(cfg):
    if not cfg.configured:
        sys.exit("Not set up yet. Run:  sonicdoctor setup")


def cmd_setup(args, cfg):
    setup_wizard.run(cfg)


def cmd_check(args, cfg):
    print(f"Plex Sonic Doctor {__version__}")
    print(f"Settings: {config.config_path()}")
    print(f"Data folder: {config.data_dir()}")
    if not cfg.configured:
        print("Plex: not set up (run  sonicdoctor setup)")
    else:
        try:
            server = plex.connect(cfg.plex_url, cfg.plex_token, timeout=20)
            print(f"Plex: connected to {server.friendlyName} ({server.version})")
        except Exception as e:
            print(f"Plex: can't connect to {cfg.plex_url}: {type(e).__name__}")
    print(f"Sonic analysis: {cfg.sonic_dir or 'over the network (no folder set)'}"
          + ("" if not cfg.sonic_dir or os.path.isdir(cfg.sonic_dir) else "  <- NOT FOUND"))
    print(f"Distance scale: {cfg.plex_scale} ({'measured on this library' if cfg.scale_measured else 'default'})")
    print("Path maps: " + (", ".join(f"{m.plex} -> {m.local}" for m in cfg.path_maps) or "none"))
    print("Fingerprints: " + setup_wizard.tools_status(cfg))
    print(f"Rules: {cfg.rules}")
    print("AcoustID: " + ("key set (tags can be checked)" if cfg.acoustid_key else "no key (tags aren't checked)"))
    print("MusicBrainz Picard: " + (" ".join(acoustid.picard_command(cfg.picard) or []) or "not found. " + acoustid.picard_help()))


def cmd_scan(args, cfg):
    _need_setup(cfg)
    res, paths = scan.run_scan(cfg, _progress, refresh=not args.quick)
    from .reports import summary_text
    print("\n" + summary_text(res))
    print(f"\nReports are in {config.sub_dir('reports')}")


def cmd_damage(args, cfg):
    _need_setup(cfg)
    n = scan.run_damage_check(cfg, args.folder or "", _progress)
    print(f"Checked {n:,} files. Run  sonicdoctor scan --quick  to add the results to the broken-files report.")


def cmd_tags(args, cfg):
    if args.key is not None:
        cfg.acoustid_key = args.key.strip()
        config.save(cfg)
        print("AcoustID key saved." if cfg.acoustid_key else "AcoustID key removed: tracks won't be looked up.")
        if not cfg.acoustid_key:
            return
    if not cfg.acoustid_key:
        sys.exit(f"Looking tracks up needs a free AcoustID key: get one at {acoustid.SIGN_UP_URL}, then run\n"
                 "  sonicdoctor tags --key YOUR_KEY")
    print("Sends each looked-up file's audio fingerprint and length to AcoustID (no names, paths or tags).")
    try:
        stats = scan.run_tag_check(cfg, args.all, _progress)
    except acoustid.BadKey as e:
        sys.exit(str(e))
    print("\n" + acoustid.summary_text(stats))
    print(f"\nTracks to fix are in {os.path.join(config.sub_dir('reports'), 'tag_issues.csv')}; "
          "fix them in MusicBrainz Picard.")


def cmd_move(args, cfg):
    rows, warning = mover.read_decisions(args.decisions)
    if warning:
        print("Note: " + warning)
    if rows and not {"group", "file", "decision"} <= set(rows[0]):
        sys.exit("The file needs 'group', 'file' and 'decision' columns (use one of the reports).")
    plan = mover.plan_moves(rows, cfg.holding_dir)
    print(f"{len(plan.ready)} files to move ({mover.fmt_size(plan.total)}), from {args.decisions}")
    for item in plan.ready[:15]:
        print(f"  {item['src']}\n    -> {item['dst']}")
    if len(plan.ready) > 15:
        print(f"  ... and {len(plan.ready) - 15} more")
    for p in plan.problems:
        print("  ! " + p)
    if not args.go:
        print("\nDry run: nothing moved. Add --go to move them.")
        return
    result = mover.execute_moves(plan, config.sub_dir("moves"), cfg.holding_dir, _progress)
    for f in result["failed"]:
        print("  ! " + f)
    print(f"\nBatch {result['batch']}: moved {result['moved']} files.")
    if (args.update_playlists or cfg.update_playlists) and result["moved"]:
        held = mover.moved_files(config.sub_dir("moves"))
        moved = {i["src"] for i in plan.ready} & held
        server = plex.connect(cfg.plex_url, cfg.plex_token)
        edits = plexlists.plan(server, cfg.sections, plexlists.replacements(rows, moved))
        pl = plexlists.apply(server, edits, _playlist_log(result["batch"]), _progress)
        for f in pl["failed"]:
            print("  ! " + f)
        print(f"Updated {pl['updated']} playlist/collection entries.")
    print(f"To put them back:  sonicdoctor undo {result['batch']}")


def _playlist_log(batch):
    return os.path.join(config.sub_dir("moves"), "playlists", batch + ".jsonl")


def cmd_batches(args, cfg):
    bs = mover.batches(config.sub_dir("moves"))
    if not bs:
        print("No batches have been moved.")
    for b in bs:
        print(f"{b['batch']}: {b['held']} files in holding ({mover.fmt_size(b['held_bytes'])}), {b['restored']} put back")


def cmd_undo(args, cfg):
    if not args.batch and not args.from_folder:
        sys.exit("undo needs a batch name (see  sonicdoctor batches) or --from-folder")
    plan = mover.plan_undo(config.sub_dir("moves"), args.batch or "", args.from_folder or "", args.match or "")
    print(f"Batch {plan.batch}: {len(plan.ready)} files to put back ({mover.fmt_size(sum(r['size'] for r in plan.ready))})")
    for r in plan.ready[:15]:
        print(f"  {r['held']}\n    -> {r['original']}")
    if len(plan.ready) > 15:
        print(f"  ... and {len(plan.ready) - 15} more")
    for p in plan.problems:
        print("  ! " + p)
    if not args.go:
        print("\nDry run: nothing moved. Add --go to put them back.")
        return
    result = mover.execute_undo(plan, _progress)
    for f in result["failed"]:
        print("  ! " + f)
    print(f"\nPut back {result['restored']} files.")
    log = _playlist_log(plan.batch)
    restored = {r["original"] for r in plan.ready} - mover.moved_files(config.sub_dir("moves"))
    if plexlists.read_log(log) and restored:
        server = plex.connect(cfg.plex_url, cfg.plex_token)
        by_file = {}

        def find_by_file(path):
            if not by_file:
                by_file.update({t["file"]: t["id"] for t in plex.fetch_tracks(server, cfg.sections, PathMapper(cfg.path_maps))})
            return by_file.get(path)
        pl = plexlists.undo(server, log, restored, find_by_file, _progress)
        for f in pl["failed"] + pl["pending"]:
            print("  ! " + f)
        print(f"Restored {pl['restored']} playlist/collection entries."
              + (" Run  sonicdoctor rescan , then this undo again, for the rest." if pl["pending"] else ""))


def cmd_rescan(args, cfg):
    _need_setup(cfg)
    plex.request_rescan(plex.connect(cfg.plex_url, cfg.plex_token), cfg.sections)
    print("Asked Plex to rescan the music libraries.")


def cmd_gui(args, cfg):
    try:
        from .gui import server as gui_server
    except ImportError:
        sys.exit("The GUI isn't available in this version yet.")
    gui_server.main(port=args.port, open_browser=not args.no_browser)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sonicdoctor", description=(
        "Find duplicate, different-version and broken tracks in a Plex music library, using Plex's own "
        "sonic analysis. Reads your library; only 'move --go' and 'undo --go' change anything, "
        "and every move can be undone."))
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="connect to Plex and find your files (run this first)")
    sub.add_parser("check", help="show settings and whether everything is reachable")
    s = sub.add_parser("scan", help="scan the library and write the reports")
    s.add_argument("--quick", action="store_true", help="reuse the last track list (e.g. after changing rules)")
    d = sub.add_parser("damage-check", help="decode every file looking for corruption (slow; can be stopped and resumed)")
    d.add_argument("--folder", help="only files whose path contains this text")
    tg = sub.add_parser("tags", help="find tracks with missing or wrong tags, using AcoustID (sends fingerprints only)")
    tg.add_argument("--all", action="store_true", help="every track, not just those whose tags look wrong (slow)")
    tg.add_argument("--key", help="save your AcoustID key first (an empty value removes it)")
    m =sub.add_parser("move", help="move the copies marked 'remove' in a report into the holding folder")
    m.add_argument("decisions", help="a report CSV with the decision column filled in")
    m.add_argument("--go", action="store_true", help="actually move (default is a dry run)")
    m.add_argument("--update-playlists", action="store_true",
                   help="put the kept copy in each moved copy's place in playlists and collections")
    sub.add_parser("batches", help="list moved batches")
    u = sub.add_parser("undo", help="put a moved batch back where it came from")
    u.add_argument("batch", nargs="?")
    u.add_argument("--match", help="only files whose original path contains this text")
    u.add_argument("--from-folder", help="rebuild the batch from its holding folder if its log is lost")
    u.add_argument("--go", action="store_true", help="actually move (default is a dry run)")
    sub.add_parser("rescan", help="ask Plex to rescan the music libraries")
    g = sub.add_parser("gui", help="open the app in your web browser")
    g.add_argument("--port", type=int, default=8765)
    g.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = config.load()
    {"setup": cmd_setup, "check": cmd_check, "scan": cmd_scan, "damage-check": cmd_damage, "tags": cmd_tags, "move": cmd_move,
     "batches": cmd_batches, "undo": cmd_undo, "rescan": cmd_rescan, "gui": cmd_gui}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
