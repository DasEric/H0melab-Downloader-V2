"""Soulseek music search and downloads through an slskd instance.

slskd (https://github.com/slskd/slskd) is the maintained Soulseek client with a
REST API. It keeps the Soulseek connection, the shares that the network expects
from every user and the transfers alive, so this module only translates between
its API and what the music page needs: grouped folders ("releases") instead of
thousands of loose file hits, and a compact transfer list.
"""

import hashlib
import os
import posixpath
import re
import shutil
import threading
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import niquests

from ..logger import get_logger

logger = get_logger(__name__)

KEYS = {
    "url": "H0MELAB_SLSKD_URL",
    "api_key": "H0MELAB_SLSKD_API_KEY",
    "music_path": "H0MELAB_MUSIC_PATH",
}

AUDIO_EXTENSIONS = {
    "aac", "aif", "aiff", "alac", "ape", "dsf", "flac", "m4a", "mp3",
    "ogg", "opus", "wav", "wma", "wv",
}
LOSSLESS_EXTENSIONS = {"aif", "aiff", "alac", "ape", "dsf", "flac", "wav", "wv"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}
IMAGE_TYPES = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
}
# Names people give the album art, best guess first
COVER_NAMES = ("cover", "folder", "front", "albumart", "album", "artwork")
MAX_COVER_BYTES = 15 * 1024 * 1024
_COVER_KEY = re.compile(r"[0-9a-f]{40}")

SEARCH_TIMEOUT_MS = 15000
SEARCH_RESPONSE_LIMIT = 250
SEARCH_FILE_LIMIT = 20000
MAX_RELEASES = 150
MAX_DOWNLOAD_FILES = 500
_TIMEOUT = 15
_SEARCH_ID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


class SoulseekError(RuntimeError):
    """slskd is not configured, unreachable or refused a request."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def settings():
    return {
        "url": os.environ.get(KEYS["url"], "").strip(),
        "key_set": bool(os.environ.get(KEYS["api_key"], "").strip()),
        "music_path": os.environ.get(KEYS["music_path"], "").strip(),
        "music_path_effective": str(music_path()),
    }


def music_path():
    """Where slskd's download folder shows up inside this container/host.

    slskd stores a download as <downloads>/<remote folder name>/<file>. Covers
    are read from there, so this has to be the same folder, mounted here.
    """
    from . import paths

    raw = os.environ.get(KEYS["music_path"], "").strip()
    return paths.expand(raw) if raw else paths.default_download_path() / "Music"


def configured():
    current = settings()
    return bool(current["url"] and current["key_set"])


def _validate_url(value):
    from .settings_store import SettingsError

    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme in ("http", "https")
            and parts.hostname
            and not (parts.username or parts.password or parts.query or parts.fragment)
        )
        _ = parts.port
    except ValueError:
        valid = False
    if not valid:
        raise SettingsError(
            "slskd URL must be HTTP(S), without credentials, query or fragment"
        )
    return value.rstrip("/")


def collect_settings(payload, updates):
    from .settings_store import SECRET_PLACEHOLDER, SettingsError

    if not isinstance(payload, dict):
        raise SettingsError("Soulseek settings must be an object")
    for name, key in KEYS.items():
        if name not in payload:
            continue
        value = payload[name]
        if not isinstance(value, str):
            raise SettingsError("Invalid Soulseek setting")
        value = value.strip()
        if name == "api_key" and value == SECRET_PLACEHOLDER:
            continue
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise SettingsError("Invalid Soulseek setting")
        if name == "url" and value:
            value = _validate_url(value)
        elif name == "api_key" and (len(value) > 512 or not value.isascii()):
            raise SettingsError("Invalid slskd API key")
        elif name == "music_path" and len(value) > 1024:
            raise SettingsError("Music folder path is too long")
        updates[key] = value


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _connection(override=None):
    """(base_url, api_key) from the saved settings, optionally overridden."""
    url = os.environ.get(KEYS["url"], "").strip()
    key = os.environ.get(KEYS["api_key"], "").strip()
    if override:
        updates = {}
        collect_settings(override, updates)
        url = updates.get(KEYS["url"], url)
        key = updates.get(KEYS["api_key"], key)
    if not url or not key:
        raise SoulseekError("Soulseek (slskd) is not configured", status=409)
    return url.rstrip("/"), key


def _request(method, path, *, json=None, params=None, connection=None):
    base, key = connection or _connection()
    try:
        response = niquests.request(
            method,
            f"{base}/api/v0{path}",
            headers={"X-API-Key": key, "Accept": "application/json"},
            json=json,
            params=params,
            timeout=_TIMEOUT,
            allow_redirects=False,
        )
    except Exception as exc:  # niquests raises several transport types
        logger.warning("slskd request %s %s failed: %s", method, path, exc)
        raise SoulseekError("slskd is not reachable") from None
    status = response.status_code or 0
    if status in (401, 403):
        raise SoulseekError("slskd rejected the API key", status=502)
    if status == 404:
        raise SoulseekError("Not found in slskd", status=404)
    if status >= 400:
        detail = (response.text or "").strip()[:200]
        logger.warning("slskd %s %s -> %s %s", method, path, status, detail)
        raise SoulseekError(detail or f"slskd answered {status}", status=502)
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def status(override=None):
    """Connection state of slskd and its Soulseek session."""
    connection = _connection(override)
    data = _request("GET", "/server", connection=connection) or {}
    state = str(data.get("state") or "")
    return {
        "reachable": True,
        "connected": bool(data.get("isConnected") or "Connected" in state),
        "logged_in": bool(data.get("isLoggedIn") or "LoggedIn" in state),
        "state": state,
        "username": (data.get("username") or ""),
    }


# ---------------------------------------------------------------------------
# Searches
# ---------------------------------------------------------------------------
def start_search(text):
    text = " ".join(str(text or "").split())
    if len(text) < 2:
        raise SoulseekError("Search needs at least two characters", status=400)
    if len(text) > 200:
        raise SoulseekError("Search text is too long", status=400)
    search_id = str(uuid.uuid4())
    _request(
        "POST",
        "/searches",
        json={
            "id": search_id,
            "searchText": text,
            "searchTimeout": SEARCH_TIMEOUT_MS,
            "responseLimit": SEARCH_RESPONSE_LIMIT,
            "fileLimit": SEARCH_FILE_LIMIT,
            "filterResponses": True,
            "minimumResponseFileCount": 1,
        },
    )
    return search_id


def _check_search_id(search_id):
    if not _SEARCH_ID.fullmatch(str(search_id or "")):
        raise SoulseekError("Invalid search id", status=400)


def _split_path(filename):
    """Soulseek paths use backslashes (Windows peers) or slashes."""
    normalized = str(filename or "").replace("\\", "/")
    directory, _, name = normalized.rpartition("/")
    return directory, name


def _extension(name, fallback=""):
    ext = str(fallback or "").lower().lstrip(".")
    if not ext and "." in name:
        ext = name.rsplit(".", 1)[1].lower()
    return ext


def group_responses(responses):
    """Turn per-user file hits into per-folder releases."""
    releases = {}
    images = {}
    for response in responses or []:
        if not isinstance(response, dict):
            continue
        username = str(response.get("username") or "")
        if not username:
            continue
        free_slot = bool(response.get("hasFreeUploadSlot"))
        speed = int(response.get("uploadSpeed") or 0)
        queue = int(response.get("queueLength") or 0)
        for entry in response.get("files") or []:
            if not isinstance(entry, dict):
                continue
            filename = str(entry.get("filename") or "")
            directory, name = _split_path(filename)
            ext = _extension(name, entry.get("extension"))
            if ext in IMAGE_EXTENSIONS:
                size = int(entry.get("size") or 0)
                if 0 < size <= MAX_COVER_BYTES:
                    images.setdefault((username, directory), []).append(
                        {"filename": filename, "name": name, "size": size}
                    )
                continue
            if ext not in AUDIO_EXTENSIONS:
                continue
            key = (username, directory)
            release = releases.get(key)
            if release is None:
                folder = posixpath.basename(directory) or directory or username
                parent = posixpath.basename(posixpath.dirname(directory))
                release = releases[key] = {
                    "id": f"{len(releases)}",
                    "username": username,
                    "directory": directory,
                    "title": folder,
                    "parent": parent,
                    "free_slot": free_slot,
                    "upload_speed": speed,
                    "queue_length": queue,
                    "files": [],
                    "size": 0,
                    "length": 0,
                    "formats": set(),
                    "bitrates": [],
                }
            track = {
                "filename": filename,
                "name": name,
                "size": int(entry.get("size") or 0),
                "extension": ext,
                "bitrate": entry.get("bitRate"),
                "bit_depth": entry.get("bitDepth"),
                "sample_rate": entry.get("sampleRate"),
                "length": entry.get("length"),
            }
            release["files"].append(track)
            release["size"] += track["size"]
            release["length"] += int(track["length"] or 0)
            release["formats"].add(ext)
            if track["bitrate"]:
                release["bitrates"].append(int(track["bitrate"]))

    result = []
    for (username, directory), release in releases.items():
        release["cover"] = _pick_cover(images.get((username, directory)), username)
        release["files"].sort(key=lambda track: track["name"].lower())
        formats = sorted(release.pop("formats"))
        bitrates = release.pop("bitrates")
        release["formats"] = [fmt.upper() for fmt in formats]
        release["lossless"] = any(fmt in LOSSLESS_EXTENSIONS for fmt in formats)
        release["bitrate"] = round(sum(bitrates) / len(bitrates)) if bitrates else None
        first = release["files"][0]
        release["bit_depth"] = first["bit_depth"]
        release["sample_rate"] = first["sample_rate"]
        release["file_count"] = len(release["files"])
        result.append(release)

    # Reachable, fast, complete folders first: that is what downloads quickly
    result.sort(
        key=lambda r: (
            not r["free_slot"],
            -min(r["file_count"], 30),
            not r["lossless"],
            -r["upload_speed"],
        )
    )
    result = result[:MAX_RELEASES]
    for index, release in enumerate(result):
        release["id"] = str(index)
    return result


def _cover_rank(image):
    stem = image["name"].rsplit(".", 1)[0].lower()
    for index, word in enumerate(COVER_NAMES):
        if stem == word:
            return (0, index, -image["size"])
    for index, word in enumerate(COVER_NAMES):
        if word in stem:
            return (1, index, -image["size"])
    return (2, 0, -image["size"])


def _pick_cover(candidates, username):
    if not candidates:
        return None
    best = min(candidates, key=_cover_rank)
    key = cover_key(username, best["filename"])
    return {
        "filename": best["filename"],
        "size": best["size"],
        "key": key,
        "cached": _cache_file(key) is not None,
    }


def search_state(search_id):
    _check_search_id(search_id)
    search = _request("GET", f"/searches/{search_id}") or {}
    complete = bool(search.get("isComplete")) or "Completed" in str(search.get("state") or "")
    responses = _request("GET", f"/searches/{search_id}/responses") or []
    releases = group_responses(responses)
    return {
        "id": search_id,
        "text": search.get("searchText") or "",
        "complete": complete,
        "response_count": int(search.get("responseCount") or len(responses)),
        "file_count": int(search.get("fileCount") or 0),
        "releases": releases,
    }


def delete_search(search_id):
    _check_search_id(search_id)
    try:
        _request("DELETE", f"/searches/{search_id}")
    except SoulseekError as exc:
        if exc.status != 404:
            raise


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------
def enqueue(username, files):
    username = str(username or "").strip()
    if not username or len(username) > 128:
        raise SoulseekError("Invalid Soulseek user", status=400)
    if not isinstance(files, list) or not files:
        raise SoulseekError("No files selected", status=400)
    if len(files) > MAX_DOWNLOAD_FILES:
        raise SoulseekError("Too many files in one request", status=400)
    payload = []
    for item in files:
        if not isinstance(item, dict):
            raise SoulseekError("Invalid file entry", status=400)
        filename = item.get("filename")
        size = item.get("size", 0)
        if not isinstance(filename, str) or not filename or len(filename) > 1024:
            raise SoulseekError("Invalid file name", status=400)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SoulseekError("Invalid file size", status=400)
        payload.append({"filename": filename, "size": size})
    _request("POST", f"/transfers/downloads/{quote(username, safe='')}", json=payload)
    return len(payload)


def download(username, files):
    """Album download chosen by the user (as opposed to a cover preview)."""
    count = enqueue(username, files)
    name = str(username).strip()
    with _preview_lock:
        for item in files:
            _previews.discard((name, item.get("filename")))
    return count


def _transfer_state(raw):
    state = str(raw or "")
    if "Succeeded" in state:
        return "completed"
    if "Cancelled" in state:
        return "cancelled"
    if any(word in state for word in ("Errored", "Rejected", "TimedOut", "Failed")):
        return "failed"
    if "InProgress" in state or "Initializing" in state:
        return "running"
    return "queued"


def transfers():
    """Downloads grouped per remote folder, newest activity first."""
    data = _request("GET", "/transfers/downloads") or []
    groups = []
    for user in data:
        if not isinstance(user, dict):
            continue
        username = str(user.get("username") or "")
        for directory in user.get("directories") or []:
            if not isinstance(directory, dict):
                continue
            path = str(directory.get("directory") or "")
            files = []
            cover = None
            for entry in directory.get("files") or []:
                if not isinstance(entry, dict):
                    continue
                full = str(entry.get("filename") or "")
                _, name = _split_path(full)
                state = _transfer_state(entry.get("state"))
                if _extension(name) in IMAGE_TYPES:
                    with _preview_lock:
                        preview = (username, full) in _previews
                    if preview:
                        # Settle previews whose album dialog was closed early
                        if state == "completed":
                            _store_cover(username, full)
                        if state in ("completed", "failed", "cancelled"):
                            _discard_preview(username, full, str(entry.get("id") or ""))
                        continue
                    if cover is None:
                        if state == "completed":
                            cover = _store_cover(username, full)
                        elif _cache_file(cover_key(username, full)):
                            cover = cover_key(username, full)
                    continue
                files.append(
                    {
                        "id": str(entry.get("id") or ""),
                        "name": name,
                        "state": state,
                        "raw_state": str(entry.get("state") or ""),
                        "size": int(entry.get("size") or 0),
                        "transferred": int(entry.get("bytesTransferred") or 0),
                        "percent": float(entry.get("percentComplete") or 0),
                        "speed": float(entry.get("averageSpeed") or 0),
                        "queue_position": entry.get("placeInQueue"),
                        "requested_at": entry.get("requestedAt") or "",
                    }
                )
            if not files:
                continue
            files.sort(key=lambda f: f["name"].lower())
            size = sum(f["size"] for f in files)
            done = sum(
                f["size"] if f["state"] == "completed" else f["transferred"]
                for f in files
            )
            states = {f["state"] for f in files}
            if "running" in states:
                state = "running"
            elif "queued" in states:
                state = "queued"
            elif "failed" in states:
                state = "failed"
            elif states == {"cancelled"}:
                state = "cancelled"
            else:
                state = "completed"
            groups.append(
                {
                    "username": username,
                    "directory": path,
                    "title": posixpath.basename(path.replace("\\", "/")) or path,
                    "state": state,
                    "size": size,
                    "transferred": done,
                    "percent": round(done / size * 100, 1) if size else 0,
                    "speed": sum(f["speed"] for f in files if f["state"] == "running"),
                    "completed": sum(1 for f in files if f["state"] == "completed"),
                    "cover": cover,
                    "requested_at": max(f["requested_at"] for f in files),
                    "files": files,
                }
            )
    order = {"running": 0, "queued": 1, "failed": 2, "completed": 3, "cancelled": 4}
    groups.sort(key=lambda g: g["requested_at"], reverse=True)
    groups.sort(key=lambda g: order.get(g["state"], 9))
    return groups


def cancel(username, transfer_ids, remove=False):
    username = str(username or "").strip()
    if not username or not isinstance(transfer_ids, list) or not transfer_ids:
        raise SoulseekError("Nothing to cancel", status=400)
    for transfer_id in transfer_ids:
        if not isinstance(transfer_id, str) or not _SEARCH_ID.fullmatch(transfer_id):
            raise SoulseekError("Invalid transfer id", status=400)
    for transfer_id in transfer_ids:
        try:
            _request(
                "DELETE",
                f"/transfers/downloads/{quote(username, safe='')}/{transfer_id}",
                params={"remove": "true" if remove else "false"},
            )
        except SoulseekError as exc:
            if exc.status != 404:
                raise


def clear_finished():
    _request("DELETE", "/transfers/downloads/all/completed")


# ---------------------------------------------------------------------------
# Covers
#
# Soulseek has no thumbnails: the picture next to the tracks is just another
# file, so it is downloaded through slskd like one. A "preview" fetch (album
# opened, not downloaded) is copied into a private cache and its file and
# transfer entry are removed again, so it never shows up as a stray folder.
# ---------------------------------------------------------------------------
_preview_lock = threading.Lock()
_previews = set()  # (username, filename) fetched only to show the cover


def cover_key(username, filename):
    return hashlib.sha1(f"{username}\n{filename}".encode()).hexdigest()


def _cache_dir():
    from ..config import H0MELAB_CONFIG_DIR

    return Path(H0MELAB_CONFIG_DIR) / "music-covers"


def _cache_file(key):
    if not _COVER_KEY.fullmatch(str(key or "")):
        return None
    directory = _cache_dir()
    for ext in IMAGE_TYPES:
        candidate = directory / f"{key}.{ext}"
        if candidate.is_file():
            return candidate
    return None


def cached_cover(key):
    """(path, mimetype) of a cached cover, or None."""
    path = _cache_file(key)
    if path is None:
        return None
    return path, IMAGE_TYPES[path.suffix.lstrip(".")]


def local_file(filename):
    """Where slskd puts a remote file: <downloads>/<remote folder>/<name>."""
    directory, name = _split_path(filename)
    parent = posixpath.basename(directory)
    if not name or not parent or name in (".", "..") or parent in (".", ".."):
        return None
    root = music_path()
    candidate = root / parent / name
    try:
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


def _store_cover(username, filename):
    """Copy a downloaded picture into the cache. Returns the key or None."""
    key = cover_key(username, filename)
    if _cache_file(key):
        return key
    ext = _extension(_split_path(filename)[1])
    source = local_file(filename)
    if ext not in IMAGE_TYPES or source is None or not source.is_file():
        return None
    if source.stat().st_size > MAX_COVER_BYTES:
        return None
    target = _cache_dir() / f"{key}.{ext}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    except OSError as exc:
        logger.warning("Could not cache cover %s: %s", source, exc)
        return None
    return key


def _discard_preview(username, filename, transfer_id=None):
    """Remove what a preview fetch left behind, unless the album came after."""
    with _preview_lock:
        if (username, filename) not in _previews:
            return
        _previews.discard((username, filename))
    if transfer_id:
        try:
            _request(
                "DELETE",
                f"/transfers/downloads/{quote(username, safe='')}/{transfer_id}",
                params={"remove": "true"},
            )
        except SoulseekError:
            pass
    source = local_file(filename)
    if source is None:
        return
    folder = source.parent
    try:
        source.unlink(missing_ok=True)
        # Only tidy the folder when the preview was the only thing in it
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass


def _find_transfer(username, filename):
    try:
        data = _request("GET", f"/transfers/downloads/{quote(username, safe='')}") or {}
    except SoulseekError as exc:
        if exc.status == 404:
            return None
        raise
    for directory in data.get("directories") or []:
        for entry in directory.get("files") or []:
            if entry.get("filename") == filename:
                return entry
    return None


def fetch_cover(username, filename, size):
    """Ask for an album picture. Returns {"state": ready|pending|failed, "key"}."""
    username = str(username or "").strip()
    if not username or not isinstance(filename, str) or not filename:
        raise SoulseekError("Invalid cover request", status=400)
    if _extension(_split_path(filename)[1]) not in IMAGE_TYPES:
        raise SoulseekError("Not an image", status=400)
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_COVER_BYTES:
        raise SoulseekError("Invalid cover size", status=400)

    key = cover_key(username, filename)
    if _cache_file(key):
        return {"state": "ready", "key": key}

    transfer = _find_transfer(username, filename)
    if transfer is None:
        if _store_cover(username, filename):  # already on disk from an album
            return {"state": "ready", "key": key}
        with _preview_lock:
            _previews.add((username, filename))
        enqueue(username, [{"filename": filename, "size": size}])
        return {"state": "pending", "key": key}

    state = _transfer_state(transfer.get("state"))
    if state == "completed":
        stored = _store_cover(username, filename)
        _discard_preview(username, filename, str(transfer.get("id") or ""))
        return {"state": "ready" if stored else "failed", "key": key}
    if state in ("failed", "cancelled"):
        _discard_preview(username, filename, str(transfer.get("id") or ""))
        return {"state": "failed", "key": key}
    return {"state": "pending", "key": key}
