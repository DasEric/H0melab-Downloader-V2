"""Turn finished Soulseek downloads into MP3 files with ffmpeg.

Only files the Soulseek client reports as downloaded (and that sit inside the
music folder) are touched, so an existing library in the same folder stays as
it is. Tags and the embedded cover are carried over; the original file is
removed once the MP3 is complete.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

KEYS = {
    "enabled": "H0MELAB_MUSIC_CONVERT_MP3",
    "bitrate": "H0MELAB_MUSIC_MP3_BITRATE",
}
BITRATES = ("128k", "192k", "256k", "320k", "V0")
DEFAULT_BITRATE = "320k"
# Lossy and lossless formats ffmpeg can read; MP3 itself is left alone
AUDIO_EXTENSIONS = frozenset({
    ".flac", ".wav", ".aif", ".aiff", ".alac", ".ape", ".wv", ".m4a", ".mp4", ".aac",
    ".ogg", ".oga", ".opus", ".wma", ".dsf", ".dff", ".mpc", ".tta",
})
SCAN_SECONDS = 10
_TIMEOUT = 30 * 60

_lock = threading.Lock()
_converting: set[str] = set()
_failed: set[str] = set()
_thread: threading.Thread | None = None
_stop = threading.Event()


def enabled():
    return os.environ.get(KEYS["enabled"], "1") == "1"


def bitrate():
    value = os.environ.get(KEYS["bitrate"], DEFAULT_BITRATE).strip()
    return value if value in BITRATES else DEFAULT_BITRATE


def settings():
    return {"convert_mp3": enabled(), "mp3_bitrate": bitrate(), "ffmpeg_found": bool(_ffmpeg())}


def collect_settings(payload, updates):
    from .settings_store import SettingsError

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


def pending_files(downloads):
    """Finished downloads (service listing) that still need converting."""
    from . import soulseek

    for user in downloads or []:
        if not isinstance(user, dict):
            continue
        for directory in user.get("directories") or []:
            if not isinstance(directory, dict):
                continue
            for entry in directory.get("files") or []:
                if not isinstance(entry, dict) or soulseek._transfer_state(entry.get("state")) != "completed":
                    continue
                local = soulseek._inside_music(entry.get("localPath"))
                if local is None or not needs_conversion(local) or not local.is_file():
                    continue
                with _lock:
                    if str(local) in _failed or str(local) in _converting:
                        continue
                yield local


def convert_finished():
    """One pass over the finished downloads. Returns how many were converted."""
    from . import soulseek

    if not enabled():
        return 0
    try:
        downloads = soulseek._request("GET", "/transfers/downloads")
    except soulseek.SoulseekError:
        return 0
    done = 0
    for path in list(pending_files(downloads)):
        key = str(path)
        with _lock:
            _converting.add(key)
        try:
            if convert(path) is not None:
                done += 1
        except Exception as exc:
            logger.warning("could not convert %s to MP3: %s", path.name, exc)
            with _lock:
                _failed.add(key)
        finally:
            with _lock:
                _converting.discard(key)
    return done


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
