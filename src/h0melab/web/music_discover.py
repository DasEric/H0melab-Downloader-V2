"""Discover rows for the music start page.

Soulseek has no charts or catalogue of its own: every user just shares
folders. The public Deezer API (no account, no key) supplies what is popular
and new; picking an entry runs a normal Soulseek search for it.
"""

import threading
import time

from ..catalog.common import CatalogError, get_json
from ..logger import get_logger

logger = get_logger(__name__)

API = "https://api.deezer.com"
HOSTS = ("api.deezer.com",)
LIMIT = 12
TTL = 60 * 60

# row -> (path, kind)
ROWS = {
    # editorial/0/releases came back empty; the editors' selection is the
    # current "new and noteworthy" list
    "new_releases": ("/editorial/0/selection", "album"),
    "top_albums": ("/chart/0/albums", "album"),
    "top_tracks": ("/chart/0/tracks", "track"),
    "top_artists": ("/chart/0/artists", "artist"),
}

_cache = {}
_lock = threading.Lock()


class DiscoverError(RuntimeError):
    pass


def _album(entry):
    artist = (entry.get("artist") or {}).get("name") or ""
    title = entry.get("title") or ""
    if not title:
        return None
    return {
        "kind": "album",
        "title": title,
        "subtitle": artist,
        "year": str(entry.get("release_date") or "")[:4],
        "cover_url": entry.get("cover_xl") or entry.get("cover_big") or entry.get("cover_medium") or "",
        "query": f"{artist} {title}".strip(),
    }


def _track(entry):
    artist = (entry.get("artist") or {}).get("name") or ""
    album = entry.get("album") or {}
    title = entry.get("title_short") or entry.get("title") or ""
    if not title:
        return None
    return {
        "kind": "track",
        "title": title,
        "subtitle": artist,
        "year": "",
        "cover_url": album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium") or "",
        "query": f"{artist} {title}".strip(),
    }


def _artist(entry):
    name = entry.get("name") or ""
    if not name:
        return None
    return {
        "kind": "artist",
        "title": name,
        "subtitle": "",
        "year": "",
        "cover_url": entry.get("picture_xl") or entry.get("picture_big") or entry.get("picture_medium") or "",
        "query": name,
    }


def items(row):
    if row not in ROWS:
        raise DiscoverError("Unknown row")
    now = time.monotonic()
    with _lock:
        cached = _cache.get(row)
        if cached and now - cached[0] < TTL:
            return cached[1]
    path, kind = ROWS[row]
    try:
        data = get_json(f"{API}{path}?limit={LIMIT}", allowed_hosts=HOSTS)
    except CatalogError as exc:
        logger.warning("Music discover row %s failed: %s", row, exc)
        if cached:
            return cached[1]  # stale beats empty
        raise DiscoverError("Discover is unavailable right now") from None
    convert = {"album": _album, "track": _track, "artist": _artist}[kind]
    result = []
    for entry in (data or {}).get("data") or []:
        if isinstance(entry, dict):
            item = convert(entry)
            if item:
                result.append(item)
    with _lock:
        _cache[row] = (now, result)
    return result
