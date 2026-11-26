"""Durable, exact-file receipts for the authenticated Jellyfin connector.

The Jellyfin connector owns the user ACL. This module never infers ownership
from a folder name and never deletes a whole title directory. Generic path,
locking and stream checks live in ``media_files`` and are shared with Seerr.
"""

import hashlib
import json
import os
import uuid
from pathlib import Path

from . import db, media_files, paths

# Re-exported so connector code keeps one import for its file helpers.
checked_path = media_files.checked_path
file_operation = media_files.file_operation
audio_matches = media_files.audio_matches

LANGUAGES = ("German Dub", "English Dub", "German Sub", "English Sub", "Japanese")
MAX_ENTRIES = 20000


def media_streams(path):
    return media_files.media_streams(path)


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
        "max_entries": MAX_ENTRIES,
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
        # A retry of the same entry keeps the ownership it already proved.
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
                elif not _same_root(row):
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


def _same_root(row):
    """A receipt stays valid only while its mount still points at the same folder.

    Routed downloads record the resolved central target, other receipts the
    configured path, so both spellings of the same folder are accepted.
    """
    current = root_for(row["custom_path_id"])
    recorded = Path(row["root"])
    return os.path.normcase(str(current)) == os.path.normcase(str(recorded)) or (
        os.path.normcase(str(current.resolve()))
        == os.path.normcase(str(recorded.resolve()))
    )


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
            if not _same_root(row):
                raise ValueError("Download root has changed")
            root = Path(row["root"])
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
