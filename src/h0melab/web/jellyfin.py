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
    from .settings_store import SECRET_PLACEHOLDER, SettingsError

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


def _auth_headers(token):
    """Use the supported scheme, including when legacy auth is disabled."""
    escaped = token.replace("\\", "\\\\").replace('"', '\\"')
    return {"Authorization": f'MediaBrowser Token="{escaped}"'}


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
            headers = _auth_headers(token)
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


def libraries(payload):
    """Read library choices using draft credentials; never persist or echo keys."""
    from .settings_store import SettingsError

    if not isinstance(payload, dict):
        raise SettingsError("Jellyfin settings must be an object")
    raw = {
        name: payload.get(name, os.environ.get(KEYS[name], ""))
        for name in ("url", "api_key")
    }
    validated = {}
    collect_settings(raw, validated)
    url = validated.get(KEYS["url"], "")
    token = validated.get(KEYS["api_key"], "")
    if not url or not token:
        raise SettingsError("Jellyfin-Server-URL und API-Key erforderlich")
    try:
        with niquests.Session() as session:
            response = session.get(
                url + "/Library/VirtualFolders",
                headers=_auth_headers(token),
                timeout=10,
                allow_redirects=False,
            )
            code = response.status_code
            if code in (401, 403):
                raise SettingsError(
                    f"Jellyfin antwortet mit HTTP {code}: API-Key oder Berechtigung abgewiesen."
                )
            if 300 <= code < 400:
                raise SettingsError(
                    f"HTTP {code}: Server leitet weiter. Die endgültige Jellyfin-URL inklusive Base-Path eintragen."
                )
            if code == 404:
                raise SettingsError(
                    "HTTP 404: Jellyfin-API nicht gefunden. Server-Port und Base-Path prüfen; nicht die /web-Adresse verwenden."
                )
            if code != 200:
                raise SettingsError(
                    f"Jellyfin-Bibliotheksabfrage: HTTP {code}. Server oder Reverse Proxy prüfen."
                )
            try:
                rows = response.json()
            except ValueError:
                raise SettingsError(
                    "Server liefert kein gültiges JSON. URL zeigt möglicherweise auf eine Weboberfläche oder Proxy-Anmeldeseite statt Jellyfin."
                ) from None
        if not isinstance(rows, list):
            raise SettingsError(
                "Unerwartete API-Antwort: Jellyfin-Bibliotheksliste fehlt. Server-URL und Reverse Proxy prüfen."
            )
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or row.get("CollectionType") not in (
                "movies",
                "tvshows",
            ):
                continue
            ident = row.get("ItemId")
            if not isinstance(ident, str) or not ident or ident in seen:
                continue
            collect_settings({"movie_library_id": ident}, {})
            seen.add(ident)
            name = row.get("Name")
            result.append(
                {
                    "id": ident,
                    "name": name if isinstance(name, str) and name else ident,
                    "type": row["CollectionType"],
                }
            )
        return result
    except SettingsError:
        raise
    except niquests.exceptions.SSLError:
        raise SettingsError(
            "TLS-Zertifikatsprüfung fehlgeschlagen. Zertifikat, Hostname und Zertifikatskette auf dem Downloader prüfen."
        ) from None
    except niquests.exceptions.Timeout:
        raise SettingsError(
            "Zeitüberschreitung beim Zugriff vom Downloader auf Jellyfin. NAS-IP, veröffentlichten Docker-Port und Firewall zwischen VM und NAS prüfen."
        ) from None
    except niquests.exceptions.ConnectionError:
        raise SettingsError(
            "Verbindung vom Downloader zu Jellyfin fehlgeschlagen. NAS-IP und veröffentlichten Docker-Port verwenden; localhost bezeichnet die Downloader-VM. DNS und Firewall prüfen."
        ) from None
    except Exception:
        raise SettingsError(
            "Bibliotheken konnten nicht geladen werden. Server-URL, API-Key und Erreichbarkeit prüfen."
        ) from None
