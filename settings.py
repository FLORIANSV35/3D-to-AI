"""Préférences de l'utilisateur (petit fichier JSON dans le dossier de configuration du système)."""
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
    return os.path.join(base, "DRONE TO AI", "settings.json")


def load():
    try:
        with open(path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(data):
    """Enregistre ; ne lève jamais d'erreur (dossier en lecture seule, etc.)."""
    try:
        os.makedirs(os.path.dirname(path()), exist_ok=True)
        with open(path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError:
        pass
