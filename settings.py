"""User preferences (a small JSON file in the system configuration folder)."""
import json
import os
import sys


def path():
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(base, "3D to AI", "settings.json")


def _old_path():
    """Folder used before the app was renamed ('DRONE TO AI') : read once so that no preference is lost."""
    return os.path.join(os.path.dirname(os.path.dirname(path())), "DRONE TO AI", "settings.json")


def load():
    for p in (path(), _old_path()):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            continue
    return {}


def save(data):
    """Save ; never raises (read-only folder, etc.)."""
    try:
        os.makedirs(os.path.dirname(path()), exist_ok=True)
        with open(path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass
