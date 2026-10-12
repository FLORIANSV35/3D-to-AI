# -*- mode: python ; coding: utf-8 -*-
# Build de l'app macOS :  ./build_app.sh   (ou : pyinstaller --noconfirm --clean AI-ROOM-OBJ.spec)
import os

APP_NAME = "DRONE TO AI"
VERSION = os.environ.get("APP_VERSION", "0.0.0")  # AA.MM.XX, fourni par build_app.sh (voir version.sh)

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["PIL._tkinter_finder"],
    hookspath=[],
    runtime_hooks=[],
    # l'app n'utilise que numpy + tkinter (+ Pillow facultatif) : on écarte le reste pour garder un bundle léger
    excludes=["scipy", "matplotlib", "pandas", "IPython", "jupyter", "notebook", "pytest", "sympy", "numba",
              "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "cv2", "torch", "tensorflow", "sklearn"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=APP_NAME, debug=False, strip=False, upx=False,
          console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    icon="assets/icon.icns",
    bundle_identifier="io.github.floriansv35.ai-room-obj",
    version=VERSION,
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": VERSION,
    "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        # permet « Ouvrir avec… » et le glisser d'un .obj sur l'icône
        "CFBundleDocumentTypes": [{
            "CFBundleTypeName": "Wavefront OBJ",
            "CFBundleTypeRole": "Viewer",
            "LSHandlerRank": "Alternate",
            "CFBundleTypeExtensions": ["obj"],
        }],
    },
)
