"""Direct catalog downloads with staging and media-server layout finalisation."""

from __future__ import annotations

import logging
import os
import re
import shutil
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import niquests as requests

from ..config import H0MELAB_CONFIG_DIR
from ..models.common.library_layout import (
    LibraryItem,
    media_path,
    opf_bytes,
    sidecar_paths,
)
from ..web import db, settings_store
from .common import USER_AGENT, CatalogError, ensure_public_url

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
MAX_REDIRECTS = 5
MAX_ARCHIVE_FILES = 2000
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024 * 1024
_AUDIO_EXTENSIONS = {"mp3", "m4a", "m4b", "flac", "ogg", "opus"}


def _response(url, *, headers=None):
    """Open a public URL and re-check every redirect target."""
    headers = {"User-Agent": USER_AGENT, **(headers or {})}
    for _ in range(MAX_REDIRECTS + 1):
        ensure_public_url(url)
        response = requests.get(
            url, headers=headers, timeout=30, stream=True, allow_redirects=False
        )
        if response.status_code not in (301, 302, 303, 307, 308):
            response.raise_for_status()
            return response, url
        url = urljoin(url, response.headers.get("location", ""))
        response.close()
    raise CatalogError("Too many download redirects")


def _stream(queue_id, url, target, *, expected="media", track_progress=True):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    offset = target.stat().st_size if target.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    response, final_url = _response(url, headers=headers)
    if offset and response.status_code != 206:
        offset = 0
    content_type = (response.headers.get("content-type") or "").lower()
    if "text/html" in content_type or "application/xhtml" in content_type:
        response.close()
        raise CatalogError("The source returned a web page instead of a media file")
    if (
        expected == "image"
        and content_type
        and not content_type.startswith("image/")
        and "octet-stream" not in content_type
    ):
        response.close()
        raise CatalogError("The cover URL did not return an image")
    mode = "ab" if offset else "wb"
    try:
        remaining = int(response.headers.get("content-length") or 0)
    except (TypeError, ValueError):
        remaining = 0
    total = offset + remaining if remaining else 0
    downloaded = offset
    if track_progress:
        db.update_queue_bytes(queue_id, downloaded, total)
    try:
        with open(target, mode) as output:
            for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                if db.is_queue_force_cancelled(queue_id):
                    raise CatalogError("Download cancelled")
                if chunk:
                    output.write(chunk)
                    downloaded += len(chunk)
                    if track_progress:
                        db.update_queue_bytes(queue_id, downloaded, total)
    finally:
        response.close()
    if downloaded <= 0:
        raise CatalogError("The source returned an empty file")
    if total and downloaded != total:
        raise CatalogError("The download ended before all bytes were received")
    return final_url


def _write_sidecars(final_file, item, *, identifier="", description=""):
    paths = sidecar_paths(final_file)
    paths["metadata"].write_bytes(
        opf_bytes(
            item,
            identifier=identifier or str(item.title),
            description=description,
        )
    )


def _image_extension(path):
    with open(path, "rb") as source:
        header = source.read(16)
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    if len(header) >= 12 and header[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    raise CatalogError("The downloaded cover is not a supported image")


def _write_cover(queue_id, cover_url, final_file):
    if not cover_url:
        return None
    folder = Path(final_file).parent
    partial = folder / "cover.image.part"
    try:
        _stream(queue_id, cover_url, partial, expected="image", track_progress=False)
        cover = folder / f"cover.{_image_extension(partial)}"
        os.replace(partial, cover)
        return cover
    except Exception as exc:
        partial.unlink(missing_ok=True)
        logger.warning("Cover download failed for %s: %s", final_file, exc)
        return None


def _validate_payload(path, extension):
    """Catch login/error responses carrying a misleading file extension."""
    path = Path(path)
    with open(path, "rb") as source:
        header = source.read(96)
    extension = extension.lower()
    valid = True
    if extension in {"epub", "zip"}:
        valid = header.startswith(b"PK\x03\x04")
    elif extension == "pdf":
        valid = header.startswith(b"%PDF-")
    elif extension in {"mobi", "azw3"}:
        valid = b"BOOKMOBI" in header
    elif extension == "flac":
        valid = header.startswith(b"fLaC")
    elif extension in {"ogg", "opus"}:
        valid = header.startswith(b"OggS")
    elif extension in {"m4a", "m4b"}:
        valid = len(header) >= 12 and header[4:8] == b"ftyp"
    elif extension == "mp3":
        valid = header.startswith(b"ID3") or (
            len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
        )
    if not valid:
        raise CatalogError(f"Downloaded data is not a valid {extension.upper()} file")


def _tag_audio(path, item, track=None):
    """Write scanner-friendly tags while leaving audio streams untouched."""
    try:
        from mutagen import File
    except ImportError:
        return
    audio = File(path, easy=True)
    if audio is None:
        return
    values = {
        "title": item.episode_title or Path(path).stem,
        "artist": item.author or "Unbekannter Autor",
        "albumartist": item.author or "Unbekannter Autor",
        "album": item.display_title,
        "date": str(item.year or item.episode_date or ""),
        "genre": "Podcast" if item.media_kind == "podcast" else "Audiobook",
    }
    if track:
        values["tracknumber"] = str(track)
    for key, value in values.items():
        if value:
            try:
                audio[key] = [str(value)]
            except Exception as exc:
                logger.debug("Audio tag %s is unsupported for %s: %s", key, path, exc)
    try:
        audio.save()
    except Exception:
        return


def _audio_members(archive):
    members = []
    total = 0
    for member in archive.infolist():
        if member.is_dir():
            continue
        total += member.file_size
        if len(members) >= MAX_ARCHIVE_FILES or total > MAX_ARCHIVE_BYTES:
            raise CatalogError("Audio archive is too large")
        extension = Path(member.filename).suffix.lower().lstrip(".")
        if extension in _AUDIO_EXTENSIONS:
            members.append(member)
    return members


def _install_archive(
    queue_id, archive_path, root, base_item, *, identifier="", description=""
):
    installed = []
    with zipfile.ZipFile(archive_path) as archive:
        members = _audio_members(archive)
        if not members:
            raise CatalogError("Audio archive contains no supported audio files")
        for index, member in enumerate(members, 1):
            if db.is_queue_force_cancelled(queue_id):
                raise CatalogError("Download cancelled")
            title = Path(member.filename).stem
            title = re.sub(r"^\s*\d+[._ -]*", "", title).strip() or f"Kapitel {index}"
            extension = Path(member.filename).suffix.lower().lstrip(".")
            chapter = LibraryItem(
                "audiobook",
                base_item.title,
                base_item.author,
                base_item.year,
                base_item.language,
                extension,
                title,
                track=index,
            )
            final = media_path(root, chapter)
            final.parent.mkdir(parents=True, exist_ok=True)
            temp = final.with_suffix(final.suffix + ".part")
            with archive.open(member) as source, open(temp, "wb") as output:
                shutil.copyfileobj(source, output, CHUNK_SIZE)
            _validate_payload(temp, extension)
            os.replace(temp, final)
            _tag_audio(final, chapter, index)
            installed.append(final)
    if installed:
        _write_sidecars(
            installed[0], base_item, identifier=identifier, description=description
        )
    return installed


def download_entry(queue_id, entry):
    """Download a server-resolved queue entry and return installed files."""
    item_data = entry["item"]
    asset = entry["asset"]
    kind = item_data["media_kind"]
    root = settings_store.media_library_path(kind)
    extension = asset.get("extension") or ("epub" if kind == "ebook" else "mp3")
    item = LibraryItem(
        kind,
        item_data.get("title", ""),
        item_data.get("author", ""),
        item_data.get("year", ""),
        item_data.get("language", ""),
        extension,
        item_data.get("episode_title", ""),
        item_data.get("episode_date", ""),
        item_data.get("track"),
    )
    staging = H0MELAB_CONFIG_DIR / ".catalog-staging" / str(queue_id)
    staging.mkdir(parents=True, exist_ok=True)
    partial = staging / f"payload.{extension}.part"
    _stream(queue_id, asset["url"], partial)
    _validate_payload(partial, extension)

    if extension == "zip" and kind == "audiobook":
        installed = _install_archive(
            queue_id,
            partial,
            root,
            item,
            identifier=str(item_data.get("id") or item.title),
            description=item_data.get("description", ""),
        )
        if installed:
            _write_cover(queue_id, item_data.get("cover_url", ""), installed[0])
        shutil.rmtree(staging, ignore_errors=True)
        return [str(path) for path in installed]

    final = media_path(root, item)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(partial, final)
    if kind in ("audiobook", "podcast"):
        _tag_audio(final, item, item.track)
    _write_sidecars(
        final,
        item,
        identifier=str(item_data.get("id") or item.title),
        description=item_data.get("description", ""),
    )
    _write_cover(queue_id, item_data.get("cover_url", ""), final)
    shutil.rmtree(staging, ignore_errors=True)
    return [str(final)]
