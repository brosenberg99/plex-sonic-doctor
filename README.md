# Plex Sonic Doctor

[![Tests](https://github.com/brosenberg99/plex-sonic-doctor/actions/workflows/tests.yml/badge.svg)](https://github.com/brosenberg99/plex-sonic-doctor/actions/workflows/tests.yml)

Find **duplicate**, **different-version** and **broken** tracks in a Plex music library, using the
sonic analysis Plex has already done on every track.

- **Likely identical copies**: the same recording saved twice (FLAC and MP3, an iTunes copy, a
  "(1)" copy...). A suggested copy to keep is marked for each group.
- **Different versions of the same song**: radio edits, remasters, mono mixes, live takes. Each
  row says how it differs, so you can keep the ones you want.
- **Possibly broken files**: files that decode to garbage or silence, won't open, stop early, aren't
  audio at all, are still encrypted, or a whole album where every track is the same clip.
- **Why tracks have no sonic analysis**: Plex analyses a whole album at once, and one file it can't
  decode makes it skip the entire album, every night, without saying which file. The **Not analysed**
  tab names that file.

Nothing is ever deleted. Copies you choose to remove are **moved** to a holding folder, and every
move can be undone.

## How it works

1. Plex (with a Plex Pass) gives each analysed track a 52-number "sound profile". The tool reads
   these and finds every pair of tracks that sound almost the same, which takes seconds even for
   100,000 tracks. Plex's files are copied first and never changed.
2. Each close pair is confirmed with an audio fingerprint (Chromaprint) from the start and the
   middle of both files, so tracks that merely sound alike aren't mistaken for copies.
3. Plex's own loudness analysis tells copies of one master apart from remasters and different mixes.
   It also exposes broken files: real audio can't peak at thousands of times full scale.

The results are spreadsheets (CSV) you can open in Excel, LibreOffice or Numbers.

## What you need

- A Plex Media Server with music, and **sonic analysis** turned on (needs Plex Pass;
  see [Plex's guide](https://support.plex.tv/articles/sonic-analysis-music/)).
- **Python 3.10 or newer**.
- **ffmpeg**, and **Chromaprint** if your ffmpeg doesn't include it. The installer checks.
  - Windows: `winget install Gyan.FFmpeg` (the full build includes Chromaprint)
  - macOS: `brew install ffmpeg chromaprint`
  - Debian/Ubuntu: `sudo apt install ffmpeg libchromaprint-tools`; Fedora: `sudo dnf install ffmpeg chromaprint-tools`
- To fingerprint the audio, the computer running the tool must be able to open your music files:
  the Plex server itself, or another computer with the music folder shared. Setup finds the folder
  for you.

## Install

Download this folder (on GitHub: **Code > Download ZIP**, then unzip), then:

- **Windows**: in the folder, run
  `powershell -ExecutionPolicy Bypass -File install\install-windows.ps1`
- **macOS / Linux**: in the folder, run
  `sh install/install-mac-linux.sh`

If you'd rather use Python directly: `pip install .` (or `pipx install .`) in this folder.

## Use

**The easy way: the app in your web browser.** On Windows, open **Plex Sonic Doctor** from the
Start menu; anywhere, run `sonicdoctor gui`. It runs on your own computer only. Keep its
small window open while you use it.

1. **Setup** walks you through connecting to Plex and finding your files (once).
2. **Overview**: press **Scan library**. A progress bar shows how far it is; you can stop it any time.
3. **Identical copies**, **Different versions**, **Possibly broken**, **Check by ear** and **Not analysed**
   show the results. Each has filters (search, status, notes, format, reasons). Each track has **▶ start** and
   **▶ middle** buttons that play a 30-second clip, and a 📂 button that shows the file in its folder.
4. Pick **Remove** for copies you don't want, or use **Mark the suggested copies for removal** to do a
   whole filtered list at once. Broken files and the problem files on **Not analysed** can be marked too
   (a file that's part of an identical group still keeps one copy). Decisions are saved as you go, and the
   bar at the bottom shows what's marked. The counts on every tab leave out what's already been moved.
5. **Move & undo** previews the move, then moves the marked copies to the holding folder. Every
   batch can be put back from the same page, all of it or just the files you search for.
6. **Settings**: change what counts as identical (for example, lengths within 3 s instead of 2), then
   **Quick re-sort** to see the effect in about a minute and a half.

**Or from the command line:**

```
sonicdoctor setup      # sign in to Plex and find your files (once)
sonicdoctor scan       # scan and write the reports
sonicdoctor gui        # open the app in your web browser
```

**Setup** asks you to sign in with your Plex account by entering a short code at
[plex.tv/link](https://plex.tv/link), so no password goes through the tool. It then:
- picks your music libraries,
- checks it can open your music files (if the server sees them under another name, like `E:\Music`
  on the server and `H:\Music` here, you just point at the folder),
- finds Plex's sonic analysis files,
- measures how Plex's distances work on your library.

**Plex's sonic analysis files** are in the Plex data folder, under
`Plug-in Support/Databases/Music Analysis N`. Setup looks in the usual places on Windows, macOS,
Linux, Docker and NAS installs ([Plex's list](https://support.plex.tv/articles/202915258-where-is-the-plex-media-server-data-directory-located/)).
If the server runs on another machine, share its data folder (read access is enough) and give
setup that path. Without access, the tool asks Plex over the network instead, which is slower
(roughly 30 minutes per 100,000 tracks) but gives the same pairs.

**Scan** writes these to the reports folder (`sonicdoctor check` shows where it is):

| File | What's in it |
|---|---|
| `likely_identical.csv` | One row per copy, grouped by song. `KEEP` marks the suggested copy to keep: lossless first, then higher bitrate, then proper tags, and never a broken copy. |
| `different_versions.csv` | One row per version of a song, with `how_it_differs_from_version_1`. |
| `possibly_broken.csv` | Damaged or wrong files across the whole library, with `folder_flagged` (e.g. "20 of 20 tracks in this folder") so you can tell when a whole album needs replacing. |
| `check_by_ear.csv` | Pairs whose audio only partly matches (alternate takes, variants). |
| `not_analysed.csv` | Tracks without Plex's sonic analysis (so they weren't compared), each with a `reason`: a problem file, blocked by a problem file in the same album, result thrown away by Plex (mostly clips under 30 s), or no problem found. |

The first scan fingerprints the candidate files, which can take about an hour for a large library
over a network share. Later scans only fingerprint new or changed files. `sonicdoctor scan --quick`
re-sorts the last results without asking Plex again (useful after changing a rule).

### Removing copies, and undo

1. In a report, type `remove` in the `decision` column of each copy you want gone. Other
   accepted words are `r`, `x`, `delete` and `yes`.
2. Save it as **CSV UTF-8** under a new name. The plain "CSV" format in Excel turns non-English
   letters into "?", and the tool skips those rows and tells you.
3. Preview, then move:

```
sonicdoctor move my-decisions.csv          # shows what would move; moves nothing
sonicdoctor move my-decisions.csv --go     # moves them to the holding folder
sonicdoctor batches                        # what's been moved
sonicdoctor undo <batch>                   # preview putting a batch back
sonicdoctor undo <batch> --go              # put it back (or --match "text" for some files)
sonicdoctor rescan                         # ask Plex to notice the change
```

Safety rules:
- **A song always keeps one copy.** If every copy of a song is marked, that song is skipped.
- **Nothing is ever overwritten,** in either direction.
- **Moves are logged twice:** in the app's folder and next to the moved files. Undo works even if
  one log is lost, and `--from-folder` can rebuild a batch from the holding folder alone.

The holding folder defaults to `_duplicates_holding` at the top of the music's drive, so moving
is a quick rename. Delete it yourself once you're happy; until then, everything can be put back.
After an undo, Plex treats the file as newly added, so its play count and rating may not come back.

### Keeping playlists whole

Tick **Also update playlists and collections** on the Move & undo page, or add
`--update-playlists` to `sonicdoctor move ... --go`. Then, wherever a moved copy appears in a
playlist or a track collection, the copy you kept of the same recording takes its place, in the
same position. If the kept copy is already in that list, the moved one is just taken out.

- **What it changes:** only normal playlists and track collections. Smart playlists and smart
  collections are left alone, because Plex fills those in itself.
- **What it swaps:** only identical copies. A removed radio edit isn't replaced by the album version.
- **Before you move:** the preview lists every entry that will change.
- **Undo:** putting a batch back also puts its playlist entries back where they were. If Plex
  has already rescanned and forgotten a moved track, undo finds it again by its file path. If Plex
  hasn't seen the file again yet, the batch shows a **Restore playlist entries** button: rescan,
  then press it.
- **Whose playlists:** the ones of the Plex account the app signed in with. Other Plex Home users'
  playlists aren't changed.

### Optional: full damage check

`sonicdoctor damage-check` decodes every file looking for corruption. It takes hours for a big
library, saves as it goes, and can be stopped and resumed. Run `sonicdoctor scan --quick`
afterwards to add what it finds to `possibly_broken.csv`.

## The rules, and changing them

A copy counts as **identical** to the keeper only if it passes all of these checks:

1. **The audio matches:** at least 85% fingerprint agreement, at the start and in the middle.
2. **The length** is within 2 seconds.
3. **The loudness** is within 0.5 LU. Copies of one master stay within about 0.4, even at
   128 kbps; remasters and different mixes usually differ by 1 or more.
4. **The channels match:** both mono or both stereo.
5. **The version words in the titles agree:** Live, Remix, Mix, Radio Edit, Extended,
   Instrumental, Acoustic, Demo, Mono, Stereo, Reprise, Alternate Take, Clean, Cover, Sped Up.
   These only count when they're in brackets or after a dash, and only when both titles name the
   same song. "Remaster" isn't a version word: loudness decides that, and many labelled remasters
   measure as the same master.

A copy that matches the song but fails a check is a **different version**. The settings are in
`config.json` under `rules` (`sonicdoctor check` shows where that file is):
- `identical_max_length_diff`
- `identical_max_loudness_diff`
- `split_on_title_words`: `false` puts title-only differences in the identical list, with a note

**Possibly broken** means one of these:

| Severity | Problem |
|---|---|
| Broken | Plex measured a peak over 2x full scale (the file decodes to garbage) |
| Broken | The file can't be opened (e.g. a WAV named .mp3, or an M4A missing its index) |
| Broken | The audio stops well before the length Plex reports |
| Broken | Not audio at all (e.g. a saved "404 Not Found" web page named .mp3), or an empty file |
| Broken | An unfinished iTunes download: the audio is still encrypted and the file has no key to unlock it (its finished album-mates play fine). Download it again from your iTunes purchases |
| Broken | Copy-protected (DRM), e.g. an iTunes Store purchase from before 2009 |
| Broken | A track Plex couldn't analyse that won't decode |
| Broken | The damage check found decoding errors |
| Likely broken | A normal song measured as pure digital silence |
| Likely broken | The same audio under 3+ different titles, plus a sign of a bad file: preview-length clips (exactly 30, 60 or 90 s), audio louder than -6 LUFS, or several of the titles in one album folder |
| Check | Constant full-volume noise (often noise music) |
| Check | Probably intentional silence |
| Check | The file is missing from disk |

The not-audio, encrypted and won't-decode checks run during a full scan, on the tracks Plex couldn't
analyse (Plex already decoded the rest), and are saved, so later scans only look at new or changed files.

## Privacy

Your Plex token, caches, reports and move logs stay on your computer, in your user's app-data
folder:
- Windows: `%LOCALAPPDATA%\PlexSonicDoctor`
- macOS: `~/Library/Application Support/PlexSonicDoctor`
- Linux: `~/.local/share/plex-sonic-doctor`

Set `SONICDOCTOR_HOME` to use another folder. Nothing is sent anywhere except to your own Plex
server and, when you sign in, plex.tv.

The browser app only answers this computer. Every request must carry a secret key, made fresh
each time the app starts and included in the link it opens, so other web pages can't use it.

## License

MIT; see [LICENSE](LICENSE).

## Credits and caveats

- Plex's sonic-analysis file format isn't documented. It was worked out by
  [knoopx](https://gist.github.com/knoopx/567b74342ad82eac1b97334caebb1d7a), and a Plex update
  could change it. The distance formula was checked against 4,000 of Plex's own answers.
- Audio fingerprints use [Chromaprint](https://acoustid.org/chromaprint).
- The thresholds were tuned on one large (100,000+ track) library. Start with small batches and listen when
  you're unsure.

## For developers

```
python -m venv .venv && .venv/bin/pip install -e .     # Windows: .venv\Scripts\pip
python -m unittest discover -s tests
```

The tests use only made-up data. The fingerprint tests also run against real ffmpeg/fpcalc if
they're installed.
