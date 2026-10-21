# 3D to AI

Converts a room / site scan (**OBJ**, e.g. from drone photogrammetry) or a **topographic survey downloaded from the internet**
into a **DAE (Collada)** that can be imported directly with the "SketchUp" importer of **Adamson ArrayIntelligence** (AI), with a 3D preview
to orient, place and crop the model before exporting.

> Independent project, not affiliated with Adamson. This repository contains no code or file from Adamson.
> (Formerly called *DRONE TO AI*.)

## Features
- **Smooth 3D preview** of the mesh (sampled surface, `.mtl` textures / colours when present).
  Drag = rotate · right-click + drag (or Shift + drag) = pan · wheel = zoom towards the cursor.
- **Placement** : rotation **around the XYZ frame** (the model turns like a solid around the AI origin ;
  to pivot around a point, first put the origin there). On screen **the model stays fixed and the frame pivots** :
  ±90° buttons (animated) or red / green / blue **rings** around the frame that you grab and drag (magnet every 15°), fine adjustment in degrees, automatic ground levelling,
  X / Y / Z position in meters, centering, ground at Z = 0.
- **XYZ frame = AI origin** drawn in the preview, with its labels (blue Z = *Top*, green Y = *Court*, red X = *Nose*, editable in `theme.py`) : grab it with the mouse (or use "origin = clicked point") to
  put the AI origin exactly where you want on the model.
- **Crop** : min / max box, or **polygon drawn in top view** ; the excluded part is greyed out.
- **Topographic survey from the internet** (instead of an OBJ) : you **draw a polygon on a satellite map** (Esri, IGN orthophoto
  or OpenStreetMap map ; address search, wheel zoom) or give an address + a size, and the app downloads the terrain
  and builds the 3D mesh, cut along the polygon. Free sources, no key : **IGN Géoplateforme** (France : bare terrain RGE ALTI
  or surface with buildings and trees, 1 m step), **AWS Terrain Tiles** (worldwide, 3 to 30 m) ; addresses through OpenStreetMap Nominatim.
  The 3D preview is textured with the satellite image ; the exported DAE stays untextured (AI ignores textures). Command line :
  `python3 topo.py "address" out.dae --size 600`.
- **AI-compatible DAE export** (see below), units and vertical axis converted, triangle reduction (enabled by default, target 1,000).

## Run from the source code
```bash
pip3 install -r requirements.txt
python3 app.py                 # or : python3 app.py model.obj
```
Python 3.10+ with Tk (the python.org Python for macOS is fine). Command line without interface :
```bash
python3 obj2dae.py model.obj out.dae --src-up Z --auto-level --rot 0 0 90 --center --floor
python3 obj2dae.py model.obj out.dae --polygon -40 -40  40 -40  40 0  0 0  0 40  -40 40
python3 app.py --selftest      # checks the conversion engine, without a window
```

## Download the app
### macOS
In the **Releases** tab : `3D-to-AI-V…-macOS-arm64.zip` (Apple Silicon Mac, macOS 11+). Unzip, then at the first
launch **right-click → Open** : the app is not notarized by Apple. If macOS still blocks it :
`xattr -dr com.apple.quarantine "3D to AI.app"`.

### Windows
In **Releases** : `3D-to-AI-V…-windows-x64.zip`. Unzip the folder, then run `3D to AI.exe`. The executable is not signed :
Windows SmartScreen may show "Windows protected your PC" → **More info → Run anyway**.
Dragging an `.obj` onto the executable loads it directly.
The Windows version is built automatically by GitHub Actions (`.github/workflows/windows.yml`) at every release ;
the build runs an interface test on a real Windows server and keeps a screenshot as an artifact.

## Build the app yourself
**macOS**
```bash
pip3 install -r requirements-build.txt
./build_app.sh                 # -> dist/3D to AI.app
```
The script runs the self-test, builds the app with PyInstaller, reruns the self-test **from the built app** and signs it
"ad hoc". Dragging an `.obj` onto the icon (or *Open With…*) loads it directly.

**Windows** (from Git Bash, with the python.org Python)
```bash
pip install -r requirements-build.txt
./build_windows.sh             # -> dist/3D-to-AI-V…-windows-x64.zip
```

## Updates
At startup the app checks (in the background, never blocking the interface) whether a newer version is published in the
**Releases** of the repository. If so, an "Update V…" button appears at the top right and opens the download suited to your system.
MODEL tab → **Version** : "Check for updates" (on demand) and "Check at startup" box (can be disabled ;
the preference is saved in the user's configuration folder).
The check only queries GitHub's public API (`api.github.com`), with no account or token, and sends nothing but the
HTTPS request. When launched from the source code (version "dev"), it does not run at startup ; `python3 app.py --check-update` does it by hand.

## Version number
Format **`V YY.MM.XX`** : `YY` year (2 digits), `MM` month, `XX` number of commits made during the month (reset to zero
at the start of each month). Example : `V26.10.02` = 2nd commit of October 2026. Computed by `version.sh` from the
git history, it appears in the window title and in the version of the `.app` bundle.
To publish : **commit, build (`build_app.sh`), then tag that same commit** (`git tag "$(./version.sh)"`).

## What AI expects from a DAE
Constraints observed in use and validated by a real import into AI :
- **a single `<triangles>`** : AI overwrites the indices at every group it meets and only keeps the last one ;
- **a single `VERTEX` input** per `<triangles>` (AI reads one index per `<input>` tag), `POSITION` first in `<vertices>` ;
- materials and textures are ignored ; a huge mesh can exhaust AI's memory (reduce the number of triangles).

The app writes exactly this structure ("A single triangle group" option, enabled by default).

## Files
| File | Role |
|---|---|
| `app.py` | Interface (MODEL / PLACEMENT / CROP / EXPORT tabs) |
| `preview3d.py` | 3D view : numpy rendering, XYZ frame, crop polygon |
| `obj2dae.py` | Engine : OBJ / MTL reading, rotation, crop, reduction, DAE writing |
| `topo.py`, `mapview.py` | Topographic survey from the internet, interactive satellite map |
| `theme.py` | Visual theme inspired by ArrayIntelligence |
| `updater.py`, `settings.py` | Update check (GitHub API) and preferences |
| `build_app.sh`, `3D-to-AI.spec`, `assets/` | Build of the macOS app and icon |
| `version.sh` | Computation of the `V YY.MM.XX` number |
