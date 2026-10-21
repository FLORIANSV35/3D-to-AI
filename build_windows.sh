#!/bin/bash
# Builds 'dist/3D-to-AI-V…-windows-x64.zip' (Windows). Run it from Git Bash, or by GitHub Actions.
# Prerequisite : Python 3 with Tk (python.org), then :  pip install -r requirements-build.txt
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python}"

export APP_VERSION="$(./version.sh --plain)"        # V YY.MM.XX computed from git
printf 'VERSION = "V%s"\n' "$APP_VERSION" > _version.py
echo "→ version V$APP_VERSION"

echo "→ self-tests (engine, then real interface + screenshot)"
"$PY" app.py --selftest
mkdir -p dist
"$PY" app.py --smoketest --screenshot dist/screenshot-windows.png

[ -f assets/icon.ico ] || "$PY" assets/make_icon.py

echo "→ PyInstaller"
"$PY" -m PyInstaller --noconfirm --clean 3D-to-AI.spec

echo "→ self-test of the built executable"
"dist/3D to AI/3D to AI.exe" --selftest

# the update check must work from the executable (embedded SSL certificates) ; non-blocking : depends on the network
"dist/3D to AI/3D to AI.exe" --check-update && echo "✔ update check : OK from the executable" \
  || echo "⚠ update check : exit code $? (network or certificates ?)"

ZIP="3D-to-AI-V${APP_VERSION}-windows-x64"
"$PY" -c "import shutil; shutil.make_archive('dist/$ZIP', 'zip', 'dist', '3D to AI')"
echo "✔ dist/$ZIP.zip"
