"""Jellyfin/Plex compatible paths for books, audiobooks and podcasts.

The configured directory is already the library root.  This module therefore
never adds another ``Books``/``Audiobooks`` directory below it; it only creates
the author/show and title folders expected by media scanners.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

MEDIA_KINDS = ("ebook", "audiobook", "podcast")
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SPACE = re.compile(r"\s+")
_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_component(value: object, fallback: str, max_length: int = 120) -> str:
    """Return one portable path component without changing meaningful Unicode."""
    cleaned = unicodedata.normalize("NFC", str(value or ""))
    cleaned = _SPACE.sub(" ", _BAD.sub("-", cleaned)).strip(" .-")
    cleaned = cleaned or fallback
    if cleaned.upper() in _WINDOWS_NAMES:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .-")
    return cleaned or fallback


def safe_extension(value: object, fallback: str) -> str:
    extension = str(value or "").lower().strip().lstrip(".")
    if not re.fullmatch(r"[a-z0-9]{1,8}", extension):
        extension = fallback.lower().lstrip(".")
    return extension


def _inside(root: Path, candidate: Path) -> Path:
    root = root.expanduser().resolve(strict=False)
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Generated media path leaves the configured library") from exc
    return candidate


@dataclass(frozen=True)
class LibraryItem:
    media_kind: str
    title: str
    author: str = ""
    year: str = ""
    language: str = ""
    extension: str = ""
    episode_title: str = ""
    episode_date: str = ""
    track: int | None = None

    @property
    def display_title(self) -> str:
        title = safe_component(self.title, "Unbekannter Titel")
        year = re.sub(r"[^0-9]", "", str(self.year or ""))[:4]
        return f"{title} ({year})" if year else title


def media_path(root: str | Path, item: LibraryItem) -> Path:
    """Build the final media file path below an existing library root."""
    if item.media_kind not in MEDIA_KINDS:
        raise ValueError(f"Unsupported media kind: {item.media_kind}")
    root = Path(root).expanduser().resolve(strict=False)

    if item.media_kind == "ebook":
        author = safe_component(item.author, "Unbekannter Autor")
        folder = root / author / item.display_title
        path = folder / f"{item.display_title}.{safe_extension(item.extension, 'epub')}"
    elif item.media_kind == "audiobook":
        author = safe_component(item.author, "Unbekannter Autor")
        folder = root / author / item.display_title
        if item.episode_title:
            number = max(0, int(item.track or 0))
            prefix = f"{number:02d} - " if number else ""
            name = prefix + safe_component(item.episode_title, item.display_title)
        else:
            name = item.display_title
        path = folder / f"{name}.{safe_extension(item.extension, 'mp3')}"
    else:
        show = safe_component(item.title, "Unbekannter Podcast")
        year = re.sub(r"[^0-9]", "", str(item.episode_date or item.year))[:4] or "Unbekannt"
        date = str(item.episode_date or "").strip()[:10]
        episode = safe_component(item.episode_title or item.title, "Unbekannte Episode")
        name = f"{date} - {episode}" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else episode
        path = root / show / year / f"{name}.{safe_extension(item.extension, 'mp3')}"
    return _inside(root, path)


def sidecar_paths(media_file: str | Path) -> dict[str, Path]:
    folder = Path(media_file).parent
    return {"cover": folder / "cover.jpg", "metadata": folder / "metadata.opf"}


def opf_bytes(item: LibraryItem, *, identifier: str = "", description: str = "") -> bytes:
    """Create a small standards-based OPF sidecar understood by Jellyfin."""
    package = ET.Element(
        "package",
        {"xmlns": "http://www.idpf.org/2007/opf", "version": "2.0", "unique-identifier": "id"},
    )
    metadata = ET.SubElement(package, "metadata", {"xmlns:dc": "http://purl.org/dc/elements/1.1/"})
    values = {
        "title": item.title,
        "creator": item.author,
        "language": item.language,
        "date": str(item.year or item.episode_date),
        "identifier": identifier,
        "description": html.unescape(description or ""),
    }
    for name, value in values.items():
        if value:
            node = ET.SubElement(metadata, f"{{http://purl.org/dc/elements/1.1/}}{name}")
            node.text = str(value)
            if name == "identifier":
                node.set("id", "id")
    return ET.tostring(package, encoding="utf-8", xml_declaration=True)
