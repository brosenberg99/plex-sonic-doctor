#!/bin/sh
# Plex Sonic Doctor installer for macOS and Linux.
#
#   sh install/install-mac-linux.sh
#
# Checks for Python 3.10+, ffmpeg and Chromaprint's fpcalc (telling you how to install what's
# missing), installs the app into its own private Python environment, and links the
# `sonicdoctor` command into ~/.local/bin.
set -e
SOURCE="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$(uname)" = "Darwin" ]; then
    INSTALL_DIR="${INSTALL_DIR:-$HOME/Library/Application Support/PlexSonicDoctor/app}"
else
    INSTALL_DIR="${INSTALL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/plex-sonic-doctor/app}"
fi
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"

echo
echo "Plex Sonic Doctor installer"
echo

PY=""
for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1 && "$cmd" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PY="$cmd"; break
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.10 or newer is needed."
    if [ "$(uname)" = "Darwin" ]; then echo "  Install it with:  brew install python   (or from https://www.python.org)"
    else echo "  Install it with your package manager, e.g.  sudo apt install python3 python3-venv"; fi
    exit 1
fi
echo "Python: OK ($($PY --version))"

missing=""
command -v ffmpeg >/dev/null 2>&1 || missing="ffmpeg"
if command -v ffmpeg >/dev/null 2>&1 && ffmpeg -hide_banner -muxers 2>/dev/null | grep -q chromaprint; then
    echo "ffmpeg with Chromaprint: OK"
elif command -v fpcalc >/dev/null 2>&1; then
    echo "fpcalc (Chromaprint): OK"
else
    missing="$missing fpcalc"
fi
if [ -n "$missing" ]; then
    echo "Missing for audio fingerprints:$missing"
    if [ "$(uname)" = "Darwin" ]; then
        echo "  Install with Homebrew:  brew install ffmpeg chromaprint"
    else
        echo "  Debian/Ubuntu:  sudo apt install ffmpeg libchromaprint-tools"
        echo "  Fedora:         sudo dnf install ffmpeg chromaprint-tools"
        echo "  Arch:           sudo pacman -S ffmpeg chromaprint"
    fi
    echo "  (You can install them after this; the app checks again when it runs.)"
fi

echo "Installing into $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
"$PY" -m venv "$INSTALL_DIR"
"$INSTALL_DIR/bin/python" -m pip install --quiet --upgrade pip
"$INSTALL_DIR/bin/python" -m pip install --quiet "$SOURCE"

mkdir -p "$BIN_DIR"
ln -sf "$INSTALL_DIR/bin/sonicdoctor" "$BIN_DIR/sonicdoctor"
"$BIN_DIR/sonicdoctor" --version
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "Note: $BIN_DIR isn't on your PATH. Add this line to your shell profile:"
       echo "  export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
echo
echo "Installed. To open the app in your web browser, run:  sonicdoctor gui"
echo "To uninstall: rm -rf \"$INSTALL_DIR\" \"$BIN_DIR/sonicdoctor\""
