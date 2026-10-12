#!/bin/bash
# Construit « dist/DRONE TO AI.app » (macOS). Prérequis : Python 3 avec Tk, puis :
#   pip3 install -r requirements-build.txt
set -euo pipefail
cd "$(dirname "$0")"

python3 -c "import numpy, tkinter" || { echo "Il manque numpy ou tkinter (pip3 install -r requirements-build.txt)"; exit 1; }
python3 -m PyInstaller --version >/dev/null 2>&1 || { echo "PyInstaller absent : pip3 install -r requirements-build.txt"; exit 1; }

# Version « V AA.MM.XX » (année, mois, nombre de commits du mois) calculée depuis git, comme tes autres projets.
# Pour publier : commiter d'abord, construire ensuite, puis taguer ce même commit.
git diff --quiet HEAD -- . 2>/dev/null || echo "⚠ modifications non commitées : la version affichée est celle du dernier commit"
export APP_VERSION="$(./version.sh --plain)"
printf 'VERSION = "V%s"\n' "$APP_VERSION" > _version.py   # lu par app.py (ignoré par git)
echo "→ version V$APP_VERSION"

echo "→ autotest du moteur de conversion"
python3 app.py --selftest

[ -f assets/icon.icns ] || python3 assets/make_icon.py

echo "→ PyInstaller"
python3 -m PyInstaller --noconfirm --clean AI-ROOM-OBJ.spec

APP="dist/DRONE TO AI.app"
echo "→ autotest de l'app construite"
"$APP/Contents/MacOS/DRONE TO AI" --selftest

# signature ad hoc (sans compte développeur) : suffisante pour lancer l'app sur CE Mac
codesign --force --deep --sign - "$APP" 2>&1 | tail -1 || true
echo "✔ $APP · V$APP_VERSION · $(du -sh "$APP" | cut -f1)"
