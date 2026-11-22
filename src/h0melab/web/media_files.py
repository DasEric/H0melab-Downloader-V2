"""Shared video output routing, filesystem validation and operation locks."""

import hashlib
import json
import os
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

from . import db, paths

_mutex = threading.RLock()
_active = set()


def checked_path(root, candidate):
    root, candidate = Path(root).absolute(), Path(candidate).absolute()
    relative = candidate.relative_to(root)
    if not relative.parts or any(part in ("..", ".") for part in relative.parts):
        raise ValueError("Invalid relative file path")
    for part in (root, *root.parents, candidate, *candidate.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("Linked directories and files are not media targets")
    candidate.resolve().relative_to(root.resolve())
    if candidate.exists() and (
        not candidate.is_file() or candidate.stat().st_nlink != 1
    ):
        raise ValueError("Expected an unshared regular file")
    return candidate


@contextmanager
def file_operation(path):
    """Coordinate downloads and deletes, including separate worker processes."""
    lock_dir = Path(db.DB_PATH).parent / "media-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(
        os.path.normcase(str(Path(path).absolute())).encode()
    ).hexdigest()
    lock_path = lock_dir / key
    with _mutex:
        if key in _active:
            raise ValueError("A download or deletion is already using this file")
        _active.add(key)
    handle = None
    locked = False
    try:
        handle = lock_path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except OSError as exc:
        raise ValueError("File operation is busy or unavailable") from exc
    finally:
        if handle is not None:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        with _mutex:
            _active.discard(key)


def media_streams(path):
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type:stream_tags=language",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(result.stdout).get("streams", [])
    except (OSError, ValueError, subprocess.SubprocessError):
        return []  # Unknown, never an invented audio language.


def audio_matches(streams, variant):
    expected = {
        "German Dub": "deu",
        "English Dub": "eng",
        "German Sub": "jpn",
        "English Sub": "jpn",
        "Japanese": "jpn",
    }.get(variant)
    normalize = {
        "de": "deu",
        "deu": "deu",
        "ger": "deu",
        "en": "eng",
        "eng": "eng",
        "ja": "jpn",
        "jpn": "jpn",
    }
    actual = {
        normalize.get(str(stream.get("tags", {}).get("language", "")).lower())
        for stream in streams
        if stream.get("codec_type") == "audio"
    }
    actual.discard(None)
    return expected in actual if expected and actual else None


def _variant_path(path, variant):
    if not variant:
        return path
    return path.with_name(
        f"{path.stem} - {paths.lang_folder_for(variant)}{path.suffix}"
    )


def _typed_video_path(root, identity, suffix, variant):
    from .video_library import output_paths, output_reservation_matches

    primary, legacy = output_paths(root, identity)
    primary = _variant_path(primary.with_suffix(suffix), variant)
    if legacy is not None and not primary.exists():
        legacy = _variant_path(legacy.with_suffix(suffix), variant)
        # An old filename alone is not proof: a same-named show can use it.
        if legacy.is_file() and output_reservation_matches(legacy, identity) is True:
            return legacy
    return primary


def typed_video_exists(root, identity, extension, variant):
    from .video_library import output_reservation_matches

    if not isinstance(extension, str) or not extension.isalnum() or len(extension) > 8:
        return False
    path = _typed_video_path(root, identity, "." + extension, variant)
    return path.is_file() and output_reservation_matches(path, identity) is not False


def episode_path(episode, path):
    """Freeze a separate variant filename without changing global templates."""
    if getattr(episode, "_seerr_output", None) is not None:
        return episode._seerr_output
    variant = getattr(episode, "_media_variant", None)
    frozen = getattr(episode, "_media_output", None) if variant else None
    if frozen is not None:
        return frozen
    identity = getattr(episode, "_video_identity", None)
    if identity:
        path = _typed_video_path(
            episode._video_root, identity, Path(path).suffix, variant
        )
    elif variant:
        path = _variant_path(Path(path), variant)
    if variant:
        episode._media_output = path
    return path


def routed_episode_path(episode):
    """Resolve typed output before legacy model templates can build a wrong path."""
    if getattr(episode, "_seerr_output", None) is not None:
        return episode._seerr_output
    if not getattr(episode, "_video_identity", None):
        return None
    extension = getattr(episode, "_file_extension", "mkv") or "mkv"
    if not isinstance(extension, str) or not extension.isalnum() or len(extension) > 8:
        raise ValueError("Invalid video file extension")
    return episode_path(episode, Path("video." + extension))
