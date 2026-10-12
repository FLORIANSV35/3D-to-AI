#!/bin/bash
# Construit « dist/DRONE-TO-AI-V…-windows-x64.zip » (Windows). À lancer depuis Git Bash, ou par GitHub Actions.
# Prérequis : Python 3 avec Tk (python.org), puis :  pip install -r requirements-build.txt
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python}"

export APP_VERSION="$(./version.sh --plain)"        # V AA.MM.XX calculée depuis git
printf 'VERSION = "V%s"\n' "$APP_VERSION" > _version.py
echo "→ version V$APP_VERSION"

echo "→ autotests (moteur, puis interface réelle + capture d'écran)"
"$PY" app.py --selftest
mkdir -p dist
"$PY" app.py --smoketest --screenshot dist/screenshot-windows.png

[ -f assets/icon.ico ] || "$PY" assets/make_icon.py

echo "→ PyInstaller"
"$PY" -m PyInstaller --noconfirm --clean DRONE-TO-ADAMSON-AI.spec

echo "→ autotest de l'exécutable construit"
"dist/DRONE TO AI/DRONE TO AI.exe" --selftest

ZIP="DRONE-TO-AI-V${APP_VERSION}-windows-x64"
"$PY" -c "import shutil; shutil.make_archive('dist/$ZIP', 'zip', 'dist', 'DRONE TO AI')"
echo "✔ dist/$ZIP.zip"
