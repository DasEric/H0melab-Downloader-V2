"""Finish Soulseek downloads: MP3 with ffmpeg, then the Jellyfin folder layout.

Only files the Soulseek client reports as downloaded (and that sit inside the
music folder) are touched, so an existing library in the same folder stays as
it is. Tags and the embedded cover are carried over; the original file is
removed once the MP3 is complete. Albums end up as
<music>/<album artist>/<album>/ with the picture as cover.jpg, as Jellyfin
expects, and the Jellyfin music library is scanned afterwards.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

KEYS = {
    "enabled": "H0MELAB_MUSIC_CONVERT_MP3",
    "bitrate": "H0MELAB_MUSIC_MP3_BITRATE",
    "organize": "H0MELAB_MUSIC_JELLYFIN_LAYOUT",
}
BITRATES = ("128k", "192k", "256k", "320k", "V0")
DEFAULT_BITRATE = "320k"
# Lossy and lossless formats ffmpeg can read; MP3 itself is left alone
AUDIO_EXTENSIONS = frozenset({
    ".flac", ".wav", ".aif", ".aiff", ".alac", ".ape", ".wv", ".m4a", ".mp4", ".aac",
    ".ogg", ".oga", ".opus", ".wma", ".dsf", ".dff", ".mpc", ".tta",
})
SCAN_SECONDS = 10
# Earlier versions saved downloads as " (1)", " (2)" … without a name or extension
_NAMELESS = re.compile(r"^\s*\(\d+\)$")
_TIMEOUT = 30 * 60
# Characters Jellyfin, Windows and SMB shares cannot take in names
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}

_lock = threading.Lock()
_converting: set[str] = set()
_failed: set[str] = set()
# (username, remote folder) -> album folder, so one album never gets split up
_albums: dict[tuple[str, str], Path] = {}
_thread: threading.Thread | None = None
_stop = threading.Event()


def enabled():
    return os.environ.get(KEYS["enabled"], "1") == "1"


def bitrate():
    value = os.environ.get(KEYS["bitrate"], DEFAULT_BITRATE).strip()
    return value if value in BITRATES else DEFAULT_BITRATE


def settings():
    return {"convert_mp3": enabled(), "mp3_bitrate": bitrate(), "ffmpeg_found": bool(_ffmpeg()),
            "jellyfin_layout": organize_enabled()}


def collect_settings(payload, updates):
    from .settings_store import SettingsError

    if "jellyfin_layout" in payload:
        updates[KEYS["organize"]] = "1" if payload["jellyfin_layout"] else "0"
    if "convert_mp3" in payload:
        updates[KEYS["enabled"]] = "1" if payload["convert_mp3"] else "0"
    if "mp3_bitrate" in payload:
        if payload["mp3_bitrate"] not in BITRATES:
            raise SettingsError("Unknown MP3 bitrate")
        updates[KEYS["bitrate"]] = payload["mp3_bitrate"]


def needs_conversion(path):
    return Path(path).suffix.lower() in AUDIO_EXTENSIONS


def is_converting(local_path):
    with _lock:
        return str(local_path) in _converting


def _ffmpeg():
    return shutil.which("ffmpeg")


def _command(ffmpeg, source, target, *, with_cover):
    quality = ["-q:a", "0"] if bitrate() == "V0" else ["-b:a", bitrate()]
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
               "-map", "0:a:0"]
    if with_cover:
        # Keep the embedded album art (FLAC/M4A pictures are image streams)
        command += ["-map", "0:v:0?", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    command += ["-c:a", "libmp3lame", *quality, "-map_metadata", "0", "-id3v2_version", "3",
                "-f", "mp3", str(target)]
    return command


def _run(command):
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.run(command, capture_output=True, text=True, timeout=_TIMEOUT,
                          check=False, creationflags=flags)


def convert(source):
    """Convert one file; returns the MP3 path, or None when it was skipped."""
    source = Path(source)
    ffmpeg = _ffmpeg()
    if ffmpeg is None:
        logger.warning("ffmpeg is missing, Soulseek downloads stay in their original format")
        return None
    target = source.with_suffix(".mp3")
    if target.exists():
        logger.info("%s already exists, keeping %s", target.name, source.name)
        return None
    partial = target.with_name(f"{target.stem}.converting.mp3")
    try:
        result = _run(_command(ffmpeg, source, partial, with_cover=True))
        if result.returncode != 0:
            # Some cover streams cannot go into ID3; the audio matters most
            result = _run(_command(ffmpeg, source, partial, with_cover=False))
        if result.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
            raise RuntimeError((result.stderr or "").strip()[-300:] or f"ffmpeg exited with {result.returncode}")
        os.replace(partial, target)
        source.unlink()
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    logger.info("converted %s to MP3", source.name)
    return target


def remote_name(remote_path):
    """The file name of a Soulseek path (they use backslashes)."""
    parts = _remote_parts(remote_path)
    return parts[-1] if parts else ""


def _remote_parts(remote_path):
    return [part.strip() for part in str(remote_path or "").replace("\\", "/").split("/") if part.strip()]


def _free_path(target):
    """target, or "name (n).ext" when something is already there."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    number = 1
    while True:
        candidate = target.with_name(f"{stem} ({number}){suffix}")
        if not candidate.exists():
            return candidate
        number += 1


def repair_name(local, remote_path):
    """Give a nameless download back its real name. Returns the usable path."""
    if not _NAMELESS.match(local.name):
        return local
    name = remote_name(remote_path)
    if not name or name in {".", ".."}:
        return local
    target = _free_path(local.with_name(name))
    os.replace(local, target)
    logger.info("renamed %s back to %s", local.name, target.name)
    return target


# ---------------------------------------------------------------------------
# Jellyfin layout: <music>/<album artist>/<album>/<tracks + cover.jpg>
# ---------------------------------------------------------------------------
def organize_enabled():
    return os.environ.get(KEYS["organize"], "1") == "1"


def _probe(path):
    """Lower-cased tags of an audio file (empty without ffprobe)."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return {}
    result = _run([ffprobe, "-v", "error", "-show_entries", "format_tags:stream_tags",
                   "-of", "json", str(path)])
    if result.returncode != 0:
        return {}
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        return {}
    tags = {}
    for stream in data.get("streams") or []:
        for key, value in (stream.get("tags") or {}).items():
            tags.setdefault(str(key).lower(), str(value))
    for key, value in ((data.get("format") or {}).get("tags") or {}).items():
        tags[str(key).lower()] = str(value)
    return tags


def _safe(name, fallback):
    """A folder name that works on Windows, Linux and network shares."""
    name = _UNSAFE.sub("_", str(name or ""))
    name = re.sub(r"\s+", " ", name).strip().strip(".").strip()[:120].rstrip(" .")
    return fallback if not name or name.upper() in _RESERVED else name


def album_folder(remote_path, tags):
    """Where an album belongs, from its tags, else from the remote folders."""
    from . import soulseek

    folders = [part for part in _remote_parts(remote_path)[:-1]
               if not part.startswith("@@") and not re.fullmatch(r"[A-Za-z]:", part)]
    artist = (tags.get("album_artist") or tags.get("albumartist") or tags.get("album artist")
              or tags.get("artist") or (folders[-2] if len(folders) >= 2 else ""))
    album = tags.get("album") or (folders[-1] if folders else "")
    return soulseek.music_path() / _safe(artist, "Unbekannter Künstler") / _safe(album, "Unbekanntes Album")


def _move(source, folder, name=None):
    folder.mkdir(parents=True, exist_ok=True)
    target = _free_path(folder / (name or source.name))
    shutil.move(str(source), str(target))
    return target


def _tidy(folder):
    """Remove the folder the client created once it is empty."""
    from . import soulseek

    try:
        if folder.resolve() != soulseek.music_path().resolve() and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    except OSError:
        pass


def organize(username, directory, tracks, images):
    """Move one album's finished files into the Jellyfin layout. Returns the count."""
    from . import soulseek

    key = (username, directory)
    moved = 0
    for remote, local in sorted(tracks, key=lambda item: item[1].name.lower()):
        with _lock:
            folder = _albums.get(key)
        if folder is None:
            folder = album_folder(remote, _probe(local))
            with _lock:
                _albums[key] = folder
        if local.parent == folder:
            continue
        _move(local, folder)
        _tidy(local.parent)
        moved += 1
    with _lock:
        folder = _albums.get(key)
    if folder is None:
        return moved
    for remote, local in images:
        if local.parent == folder:
            continue
        soulseek._store_cover(username, remote, str(local))  # keep the queue cover
        has_cover = any(folder.glob("cover.*")) or any(folder.glob("folder.*"))
        _move(local, folder, None if has_cover else f"cover{local.suffix.lower()}")
        _tidy(local.parent)
        moved += 1
    return moved


def _finished_groups(downloads):
    """(username, remote folder, [(remote path, local path)]) of finished files."""
    from . import soulseek

    for user in downloads or []:
        if not isinstance(user, dict):
            continue
        username = str(user.get("username") or "")
        for directory in user.get("directories") or []:
            if not isinstance(directory, dict):
                continue
            files = []
            for entry in directory.get("files") or []:
                if not isinstance(entry, dict) or soulseek._transfer_state(entry.get("state")) != "completed":
                    continue
                remote = str(entry.get("filename") or "")
                with soulseek._preview_lock:
                    if (username, remote) in soulseek._previews:
                        continue  # a cover preview, cleaned up elsewhere
                local = soulseek._inside_music(entry.get("localPath"))
                if local is None or not local.is_file():
                    continue
                files.append((remote, local))
            if files:
                yield username, str(directory.get("directory") or ""), files


def _convert_one(path):
    key = str(path)
    with _lock:
        if key in _failed or key in _converting:
            return None
        _converting.add(key)
    try:
        return convert(path)
    except Exception as exc:
        logger.warning("could not convert %s to MP3: %s", path.name, exc)
        with _lock:
            _failed.add(key)
        return None
    finally:
        with _lock:
            _converting.discard(key)


def convert_finished():
    """One pass over the finished downloads: name, convert, sort for Jellyfin.

    Returns how many files were converted to MP3.
    """
    from . import jellyfin, soulseek

    convert_on, organize_on = enabled(), organize_enabled()
    if not convert_on and not organize_on:
        return 0
    try:
        downloads = soulseek._request("GET", "/transfers/downloads")
    except soulseek.SoulseekError:
        return 0
    converted = moved = 0
    for username, directory, files in list(_finished_groups(downloads)):
        tracks, images = [], []
        for remote, local in files:
            try:
                local = repair_name(local, remote)
            except OSError as exc:
                logger.warning("could not rename %s: %s", local.name, exc)
                continue
            suffix = local.suffix.lower()
            if suffix.lstrip(".") in soulseek.IMAGE_TYPES:
                images.append((remote, local))
                continue
            if suffix != ".mp3" and suffix not in AUDIO_EXTENSIONS:
                continue
            if convert_on and needs_conversion(local):
                target = _convert_one(local)
                if target is not None:
                    converted += 1
                    local = target
                elif not local.exists():
                    continue
            tracks.append((remote, local))
        if organize_on and (tracks or images):
            try:
                moved += organize(username, directory, tracks, images)
            except OSError as exc:
                logger.warning("could not sort %s into the music library: %s", directory, exc)
    if moved:
        jellyfin.refresh("music")
    return converted


def _loop():
    from . import soulseek_runtime

    while not _stop.wait(SCAN_SECONDS):
        if soulseek_runtime.status().get("phase") != "running":
            continue
        try:
            convert_finished()
        except Exception:
            logger.exception("MP3 conversion pass failed")


def start():
    """Run the converter in the background (idempotent)."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="music-mp3-convert", daemon=True)
        _thread.start()
