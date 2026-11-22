"""Targeted, best-effort Jellyfin library refresh after completed video files."""

import os
import re
from urllib.parse import urlsplit

import niquests

from ..logger import get_logger

logger = get_logger(__name__)
KEYS = {
    name: "H0MELAB_JELLYFIN_" + name.upper()
    for name in ("url", "api_key", "movie_library_id", "series_library_id")
}


def settings():
    result = {
        name: os.environ.get(key, "").strip()
        for name, key in KEYS.items()
        if name != "api_key"
    }
    result["key_set"] = bool(os.environ.get(KEYS["api_key"], "").strip())
    return result


def collect_settings(payload, updates):
    from .settings_store import SettingsError, SECRET_PLACEHOLDER

    if not isinstance(payload, dict):
        raise SettingsError("Jellyfin settings must be an object")
    for name, key in KEYS.items():
        if name not in payload:
            continue
        value = payload[name]
        if not isinstance(value, str):
            raise SettingsError("Invalid Jellyfin setting")
        value = value.strip()
        if name == "api_key" and value == SECRET_PLACEHOLDER:
            continue
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise SettingsError("Invalid Jellyfin setting")
        if name == "url" and value:
            try:
                parts = urlsplit(value)
                valid = (
                    parts.scheme in ("http", "https")
                    and parts.hostname
                    and not (
                        parts.username
                        or parts.password
                        or parts.query
                        or parts.fragment
                    )
                )
                _ = parts.port
            except ValueError:
                valid = False
            if not valid:
                raise SettingsError(
                    "Jellyfin URL must be HTTP(S), without credentials, query or fragment"
                )
            value = value.rstrip("/")
        elif name.endswith("library_id") and value:
            if not re.fullmatch(
                r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}",
                value,
            ):
                raise SettingsError("Jellyfin library ID must be a UUID")
        elif name == "api_key" and (len(value) > 512 or not value.isascii()):
            raise SettingsError("Invalid Jellyfin API key")
        updates[key] = value


def refresh(media_type):
    """Queue only the configured typed library; never refresh all libraries."""
    kind = {"movie": "movie", "tv": "series", "series": "series"}.get(media_type)
    if not kind:
        return False
    try:
        raw = {name: os.environ.get(key, "") for name, key in KEYS.items()}
        validated = {}
        collect_settings(raw, validated)
        url = validated[KEYS["url"]]
        token = validated[KEYS["api_key"]]
        library = validated[KEYS[kind + "_library_id"]]
        if not url or not token or not library:
            return False
        # Verify both collection type and ID, rather than trusting swapped settings.
        with niquests.Session() as session:
            headers = {"X-Emby-Token": token}
            response = session.get(
                url + "/Library/VirtualFolders",
                headers=headers,
                timeout=10,
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise ValueError("Library discovery failed")
            folders = response.json()
            normalize = lambda value: str(value).replace("-", "").lower()
            expected = "movies" if kind == "movie" else "tvshows"
            if not isinstance(folders, list) or not any(
                isinstance(folder, dict)
                and normalize(folder.get("ItemId")) == normalize(library)
                and folder.get("CollectionType") == expected
                for folder in folders
            ):
                raise ValueError("Library type mismatch")
            response = session.post(
                url + "/Items/" + library + "/Refresh",
                headers=headers,
                params={
                    "Recursive": "true",
                    "MetadataRefreshMode": "Default",
                    "ImageRefreshMode": "Default",
                    "ReplaceAllMetadata": "false",
                    "ReplaceAllImages": "false",
                },
                timeout=10,
                allow_redirects=False,
            )
            if response.status_code not in (200, 204):
                raise ValueError("Refresh rejected")
        logger.info("Jellyfin %s library refresh requested", kind)
        return True
    except Exception:
        # Never log request exceptions: they may contain API tokens or private URLs.
        logger.warning(
            "Jellyfin %s library refresh failed; check integration settings and server availability",
            kind,
        )
        return False
