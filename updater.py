"""Recherche de mise à jour : compare la version de l'app à la dernière release publiée sur GitHub.

Interroge uniquement l'API publique de GitHub (api.github.com, sans compte ni jeton) ; aucune donnée
n'est envoyée en dehors de la requête HTTPS elle-même.
"""
import json
import platform
import re
import ssl
import sys
import urllib.request

REPO = "FLORIANSV35/DRONE-TO-ADAMSON-AI"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
_VERSION = re.compile(r"V?\s*(\d{2})\.(\d{2})\.(\d+)", re.I)  # V AA.MM.XX


def parse_version(tag):
    """'V26.10.03' -> (26, 10, 3) ; None si le texte n'est pas un numéro de version (ex. « dev »)."""
    m = _VERSION.search(tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    return a is not None and b is not None and a > b


def _context():
    """Contexte SSL avec les certificats de certifi : le Python de python.org (et les apps empaquetées)
    n'ont pas de certificats système, la vérification échouerait sinon."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def platform_asset(assets, page):
    """Lien de téléchargement adapté à ce système, sinon la page de la release."""
    key = {"darwin": "macos", "win32": "windows"}.get(sys.platform)
    if key == "macos" and platform.machine() != "arm64":
        return page  # seule une version Apple Silicon est publiée
    for a in assets or []:
        if key and key in a.get("name", "").lower():
            return a.get("browser_download_url") or page
    return page


def fetch_latest(current, timeout=6):
    """Dernière release publiée : {tag, name, date, notes, page, download, newer}. Lève une exception si GitHub
    est injoignable ou répond autre chose qu'une release."""
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"DRONE-TO-AI/{current}"})
    with urllib.request.urlopen(req, timeout=timeout, context=_context()) as r:
        data = json.load(r)
    tag = data.get("tag_name", "")
    if parse_version(tag) is None:
        raise ValueError(f"numéro de version illisible dans la release : {tag!r}")
    page = data.get("html_url", f"https://github.com/{REPO}/releases")
    return {"tag": tag, "name": data.get("name") or tag, "date": (data.get("published_at") or "")[:10],
            "notes": (data.get("body") or "").strip(), "page": page,
            "download": platform_asset(data.get("assets"), page), "newer": is_newer(tag, current)}
