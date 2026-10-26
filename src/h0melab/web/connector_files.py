"""Durable, exact-file receipts for authenticated connector requests.

The Jellyfin connector owns the user ACL. This module never infers ownership
from a folder name and never deletes a whole title directory.
"""

import hashlib
import json
import os
import subprocess
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import db, paths

_mutex = threading.RLock()
_active = set()


def _schema(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS connector_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO connector_state VALUES ('instance_id', ?)",
        (uuid.uuid4().hex,),
    )
    conn.execute("""CREATE TABLE IF NOT EXISTS connector_artifacts (
        id TEXT PRIMARY KEY, request_key TEXT NOT NULL, queue_id INTEGER NOT NULL,
        item_index INTEGER NOT NULL, root TEXT NOT NULL, relative_path TEXT NOT NULL,
        custom_path_id INTEGER, media_type TEXT NOT NULL, language TEXT NOT NULL,
        fingerprint TEXT NOT NULL, size INTEGER NOT NULL, state TEXT NOT NULL,
        streams TEXT NOT NULL DEFAULT '[]', UNIQUE(request_key, item_index, relative_path))""")


def capabilities():
    from . import media_targets

    with db.session() as conn:
        _schema(conn)
        identity = conn.execute(
            "SELECT value FROM connector_state WHERE key='instance_id'"
        ).fetchone()["value"]
    return {
        "protocol": 1,
        "instance_id": identity,
        "file_receipts": True,
        "entry_options": True,
        "video_groups": 1,
        "media_targets": media_targets.read(),
        "max_entries": 20000,
        "paths": [
            {"id": path_id, "name": name, "path": str(Path(root).absolute())}
            for name, path_id, root in paths.download_roots()
        ],
    }


def root_for(path_id):
    root = paths.custom_path_base(path_id) if path_id else paths.default_download_path()
    if root is None:
        raise ValueError("Configured download path no longer exists")
    return Path(root).absolute()


def checked_path(root, candidate):
    root, candidate = Path(root).absolute(), Path(candidate).absolute()
    relative = candidate.relative_to(root)
    if not relative.parts or any(part in ("..", ".") for part in relative.parts):
        raise ValueError("Invalid relative file path")
    for part in (root, *root.parents, candidate, *candidate.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError("Linked directories and files are not connector targets")
    candidate.resolve().relative_to(root.resolve())
    if candidate.exists() and (
        not candidate.is_file() or candidate.stat().st_nlink != 1
    ):
        raise ValueError("Expected an unshared regular file")
    return candidate


def fingerprint(path):
    with open(path, "rb") as source:
        stat = os.fstat(source.fileno())
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        after = os.fstat(source.fileno())
    if (stat.st_ino, stat.st_size, stat.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("File changed while verifying it")
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:{digest}"


@contextmanager
def file_operation(path):
    """Coordinate downloads and deletes, including separate worker processes."""
    lock_dir = Path(db.DB_PATH).parent / "connector-locks"
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


def record(item, index, path, root, existed, options):
    path = checked_path(root, path)
    if not path.exists():
        raise ValueError("Download did not produce a finished file")
    stamp = fingerprint(path)
    relative = path.relative_to(root).as_posix()
    streams = media_streams(path)
    with db.session() as conn:
        _schema(conn)
        old = conn.execute(
            "SELECT * FROM connector_artifacts WHERE request_key=? AND item_index=? AND relative_path=?",
            (item["request_key"], index, relative),
        ).fetchone()
        if old and old["state"] == "owned" and old["fingerprint"] == stamp:
            return
        conn.execute(
            """INSERT INTO connector_artifacts
            (id,request_key,queue_id,item_index,root,relative_path,custom_path_id,media_type,language,fingerprint,size,state,streams)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(request_key,item_index,relative_path) DO UPDATE SET
              id=excluded.id, state=excluded.state, fingerprint=excluded.fingerprint, size=excluded.size, streams=excluded.streams""",
            (
                uuid.uuid4().hex,
                item["request_key"],
                item["id"],
                index,
                str(root),
                relative,
                options.get("custom_path_id"),
                options.get("media_type", "series"),
                options["language"],
                stamp,
                path.stat().st_size,
                "existing" if existed else "owned",
                json.dumps(streams),
            ),
        )


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


def query(keys):
    if (
        not isinstance(keys, list)
        or not 1 <= len(keys) <= 100
        or any(not isinstance(k, str) or not 1 <= len(k) <= 128 for k in keys)
    ):
        raise ValueError("Expected 1 to 100 request keys")
    with db.session() as conn:
        _schema(conn)
        rows = conn.execute(
            f"SELECT * FROM connector_artifacts WHERE request_key IN ({','.join('?' for _ in keys)})",
            keys,
        ).fetchall()
    result = []
    for row in rows:
        state = row["state"]
        if state == "owned":
            try:
                path = checked_path(
                    row["root"], Path(row["root"]) / row["relative_path"]
                )
                if not path.exists():
                    state = "missing"
                elif str(root_for(row["custom_path_id"])) != row["root"]:
                    state = "changed"
                else:
                    stat = path.stat()
                    prefix = f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}:"
                    if not row["fingerprint"].startswith(prefix):
                        state = "changed"
            except (OSError, ValueError):
                state = "changed"
        result.append(
            {
                "id": row["id"],
                "request_key": row["request_key"],
                "name": Path(row["relative_path"]).name,
                "relative_path": row["relative_path"],
                "custom_path_id": row["custom_path_id"],
                "media_type": row["media_type"],
                "language": row["language"],
                "size": row["size"],
                "state": state,
                "streams": json.loads(row["streams"]),
            }
        )
    return result


def delete(ids):
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= 500
        or any(not isinstance(k, str) or len(k) != 32 for k in ids)
    ):
        raise ValueError("Expected 1 to 500 artifact IDs")
    results = []
    for identity in dict.fromkeys(ids):
        try:
            with db.session() as conn:
                _schema(conn)
                row = conn.execute(
                    "SELECT * FROM connector_artifacts WHERE id=?", (identity,)
                ).fetchone()
            if row is None or row["state"] not in ("owned", "deleted"):
                raise ValueError("No owned file receipt")
            if row["state"] == "deleted":
                results.append({"id": identity, "status": "deleted"})
                continue
            root = root_for(row["custom_path_id"])
            if str(root) != row["root"]:
                raise ValueError("Download root has changed")
            path = checked_path(root, root / row["relative_path"])
            with file_operation(path):
                path = checked_path(root, path)
                if path.exists():
                    if fingerprint(path) != row["fingerprint"]:
                        raise ValueError("File has been replaced or modified")
                    path.unlink()
                with db.session() as conn:
                    conn.execute(
                        "UPDATE connector_artifacts SET state='deleted' WHERE id=?",
                        (identity,),
                    )
                parent = path.parent
                while parent != root:
                    try:
                        parent.rmdir()  # Only empty directories; never recursively delete.
                    except OSError:
                        break
                    parent = parent.parent
            results.append({"id": identity, "status": "deleted"})
        except (ValueError, OSError):
            results.append({"id": identity, "status": "conflict"})
    return results


def episode_path(episode, path):
    """Freeze a separate variant filename without changing global templates."""
    identity = getattr(episode, "_video_identity", None)
    if identity:
        from .video_library import output_path

        path = output_path(episode._video_root, identity).with_suffix(Path(path).suffix)
    variant = getattr(episode, "_connector_variant", None)
    if not variant:
        return path
    frozen = getattr(episode, "_connector_output", None)
    if frozen is None:
        value = Path(path)
        frozen = value.with_name(
            f"{value.stem} - {paths.lang_folder_for(variant)}{value.suffix}"
        )
        episode._connector_output = frozen
    return frozen


def routed_episode_path(episode):
    """Resolve typed output before legacy model templates can build a wrong path."""
    if not getattr(episode, "_video_identity", None):
        return None
    extension = getattr(episode, "_file_extension", "mkv") or "mkv"
    if not isinstance(extension, str) or not extension.isalnum() or len(extension) > 8:
        raise ValueError("Invalid video file extension")
    return episode_path(episode, Path("video." + extension))
