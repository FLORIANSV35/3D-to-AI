#!/bin/bash
# Builds 'dist/3D to AI.app' (macOS). Prerequisite : Python 3 with Tk, then :
#   pip3 install -r requirements-build.txt
set -euo pipefail
cd "$(dirname "$0")"

python3 -c "import numpy, tkinter" || { echo "numpy or tkinter is missing (pip3 install -r requirements-build.txt)"; exit 1; }
python3 -m PyInstaller --version >/dev/null 2>&1 || { echo "PyInstaller missing : pip3 install -r requirements-build.txt"; exit 1; }

# Version 'V YY.MM.XX' (year, month, number of commits of the month) computed from git, like your other projects.
# To publish : commit first, build next, then tag that same commit.
git diff --quiet HEAD -- . 2>/dev/null || echo "⚠ uncommitted changes : the displayed version is the one of the last commit"
export APP_VERSION="$(./version.sh --plain)"
printf 'VERSION = "V%s"\n' "$APP_VERSION" > _version.py   # read by app.py (ignored by git)
echo "→ version V$APP_VERSION"

echo "→ self-test of the conversion engine"
python3 app.py --selftest

[ -f assets/icon.icns ] || python3 assets/make_icon.py

echo "→ PyInstaller"
python3 -m PyInstaller --noconfirm --clean 3D-to-AI.spec

APP="dist/3D to AI.app"
echo "→ self-test of the built app"
"$APP/Contents/MacOS/3D to AI" --selftest

# ad hoc signature (no developer account) : enough to launch the app on THIS Mac
codesign --force --deep --sign - "$APP" 2>&1 | tail -1 || true
echo "✔ $APP · V$APP_VERSION · $(du -sh "$APP" | cut -f1)"
