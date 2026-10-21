"""Update check : compares the app version with the latest release published on GitHub.

Only queries GitHub's public API (api.github.com, no account or token) ; no data is sent beyond the
HTTPS request itself.
"""
import json
import platform
import re
import ssl
import sys
import urllib.request

REPO = "FLORIANSV35/3D-to-AI"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
_VERSION = re.compile(r"V?\s*(\d{2})\.(\d{2})\.(\d+)", re.I)  # V AA.MM.XX


def parse_version(tag):
    """'V26.10.03' -> (26, 10, 3) ; None if the text is not a version number (e.g. 'dev')."""
    m = _VERSION.search(tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(latest, current):
    a, b = parse_version(latest), parse_version(current)
    return a is not None and b is not None and a > b


def _context():
    """SSL context with the certifi certificates : the python.org Python (and packaged apps)
    have no system certificates, verification would fail otherwise."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def platform_asset(assets, page):
    """Download link for this system, otherwise the release page."""
    key = {"darwin": "macos", "win32": "windows"}.get(sys.platform)
    if key == "macos" and platform.machine() != "arm64":
        return page  # only an Apple Silicon build is published
    for a in assets or []:
        if key and key in a.get("name", "").lower():
            return a.get("browser_download_url") or page
    return page


def fetch_latest(current, timeout=6):
    """Latest published release : {tag, name, date, notes, page, download, newer}. Raises if GitHub
    is unreachable or answers something other than a release."""
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"3D-to-AI/{current}"})
    with urllib.request.urlopen(req, timeout=timeout, context=_context()) as r:
        data = json.load(r)
    tag = data.get("tag_name", "")
    if parse_version(tag) is None:
        raise ValueError(f"unreadable version number in the release: {tag!r}")
    page = data.get("html_url", f"https://github.com/{REPO}/releases")
    return {"tag": tag, "name": data.get("name") or tag, "date": (data.get("published_at") or "")[:10],
            "notes": (data.get("body") or "").strip(), "page": page,
            "download": platform_asset(data.get("assets"), page), "newer": is_newer(tag, current)}
