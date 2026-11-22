"""TMDB-bound output layout and identity receipts; existing files never overwritten."""

import time
from pathlib import Path

from . import db, media_files, media_targets, seerr
from .seerr_matching import IdentityReviewRequired
from .video_library import safe_component


def folder(identity):
    with db.session() as conn:
        seerr.schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute(
            "SELECT folder FROM seerr_media WHERE media_type=? AND tmdb_id=?",
            (identity["media_type"], identity["tmdb_id"]),
        ).fetchone()
        if old:
            return old[0]
        name = safe_component(identity["title"], "", max_length=100)
        if not name:
            raise ValueError("Verified title has no usable filename")
        if identity["media_type"] == "movie" and identity.get("release_year"):
            name += f" ({identity['release_year']})"
        collision = conn.execute(
            "SELECT tmdb_id FROM seerr_media WHERE media_type=? AND folder=?",
            (identity["media_type"], name),
        ).fetchone()
        if collision:
            name += f" [tmdbid-{identity['tmdb_id']}]"
        conn.execute(
            "INSERT INTO seerr_media VALUES (?,?,?,?)",
            (
                identity["media_type"],
                identity["tmdb_id"],
                identity.get("imdb_id"),
                name,
            ),
        )
        return name


def output(entry, extension):
    identity = entry["_seerr_identity"]
    if not isinstance(extension, str) or not extension.isalnum() or len(extension) > 8:
        raise ValueError("Invalid video file extension")
    target = media_targets.resolve(
        "movie" if identity["media_type"] == "movie" else "series"
    )
    if not target:
        raise ValueError("Media destinations are not configured")
    root = target[1]
    name = folder(identity)
    if identity["media_type"] == "movie":
        path = root / (name + "." + extension)
    else:
        path = (
            root
            / name
            / f"Season {entry['season']:02d}"
            / f"{name} S{entry['season']:02d}E{entry['episode']:02d}.{extension}"
        )
    return root, media_files.checked_path(root, path)


def key(entry):
    identity = entry["_seerr_identity"]
    return (
        identity["media_type"],
        identity["tmdb_id"],
        entry["season"],
        entry["episode"],
    )


def existing(entry):
    with db.session() as conn:
        seerr.schema(conn)
        rows = conn.execute(
            "SELECT * FROM seerr_files WHERE media_type=? AND tmdb_id=? AND season=? AND episode=?",
            key(entry),
        ).fetchall()
    for row in rows:
        path = Path(row["path"])
        try:
            path = media_files.checked_path(path.parent, path)
            stat = path.stat()
            if (
                stat.st_size == row["size"]
                and stat.st_mtime_ns == row["mtime_ns"]
                and stat.st_size > 0
            ):
                return path
        except (OSError, ValueError):
            continue
    return None


def record(entry, path):
    path = media_files.checked_path(Path(path).parent, path)
    stat = path.stat()
    if not path.is_file() or stat.st_size <= 0:
        raise ValueError("Download did not produce a finished media file")
    with db.session() as conn:
        seerr.schema(conn)
        conn.execute(
            "INSERT OR REPLACE INTO seerr_files VALUES (?,?,?,?,?,?,?)",
            (*key(entry), str(path), stat.st_size, stat.st_mtime_ns),
        )


def download(item, entry, episode, *, source_request=None):
    identity = entry["_seerr_identity"]
    from . import video_library

    fresh = video_library.identity_for_episode(episode, entry["url"])
    expected_kind = "movie" if identity["media_type"] == "movie" else "series"
    if not fresh or fresh.get("media_type") != expected_kind:
        raise IdentityReviewRequired(
            "Provider media type is not confirmed before download"
        )
    if expected_kind == "series" and any(
        fresh.get(k) != entry.get("source_" + k, entry[k])
        for k in ("season", "episode")
    ):
        raise IdentityReviewRequired(
            "Provider episode numbering changed before download"
        )
    for field, attributes in (
        ("tmdb_id", ("tmdb_id",)),
        ("imdb_id", ("imdb_id", "imdb")),
    ):
        for attribute in attributes:
            value = getattr(episode, attribute, None)
            if value not in (None, "") and value != identity.get(field):
                raise IdentityReviewRequired(
                    "Provider external identity changed before download"
                )
    # A proven existing copy remains available even after destination changes;
    # no media is moved and no redundant copy is downloaded.
    if existing(entry):
        return True
    from . import seerr_matching

    request = seerr.get_request(queue_id=item["id"])
    request = source_request or request
    seerr_matching.revalidate_selection(identity, request)
    if expected_kind == "series":
        seerr_matching.revalidate_episode_source(identity, request, entry)
    from .seerr_processing import stopped

    if stopped(item):
        return None
    root, path = output(entry, getattr(episode, "_file_extension", "mkv") or "mkv")
    episode._seerr_output = path
    episode.selected_path = str(root)
    with media_files.file_operation(path):
        if existing(entry):
            return True
        if path.exists():
            raise IdentityReviewRequired(
                "Existing output has no matching identity receipt; manual review required"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        episode.download()
        if episode._episode_path != path:
            raise ValueError("Provider changed the verified output path")
        record(entry, path)
    request = seerr.get_request(queue_id=item["id"])
    seerr.notify(
        request["request_id"],
        f"episode_completed:{entry['season']}:{entry['episode']}",
        identity["title"],
    )
    return False


def finish(item, status, all_existing=False):
    request = seerr.get_request(queue_id=item["id"])
    final = "already_present" if status == "completed" and all_existing else status
    details = ""
    if status == "completed" and request["episode_review"].get("pending"):
        final = "partial"
        details = "Verified episodes finished; outstanding episodes require a later retry or manual review"
    if status == "failed":
        details = "One or more downloads failed; completed identity receipts are retained for retry"
    with db.session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT status,force_cancelled FROM download_queue WHERE id=?",
            (item["id"],),
        ).fetchone()
        if current is None:
            return
        if current["status"] == "cancelled" or current["force_cancelled"]:
            status = final = "cancelled"
            details = ""
        conn.execute(
            "UPDATE download_queue SET status=?,pause_requested=?,"
            "completed_at=CASE WHEN ? THEN NULL ELSE datetime('now') END WHERE id=?",
            (
                "queued" if final == "partial" else status,
                int(final == "partial"),
                int(final == "partial"),
                item["id"],
            ),
        )
        conn.execute(
            "UPDATE seerr_requests SET status=?,error=?,updated=? WHERE request_id=?",
            (final, details, time.time(), request["request_id"]),
        )
    seerr.notify(request["request_id"], final, item["title"])
