"""Persistent, server-authoritative movie/show destinations shared by all clients."""

import os
import uuid

from . import db, paths


def _schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS media_targets (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        movie INTEGER NOT NULL, series INTEGER NOT NULL, revision INTEGER NOT NULL,
        movie_root TEXT NOT NULL DEFAULT '', series_root TEXT NOT NULL DEFAULT '')""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(media_targets)")}
    for name in ("movie_root", "series_root"):
        if name not in columns:
            conn.execute(
                f"ALTER TABLE media_targets ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
            )


def read():
    with db.session() as conn:
        _schema(conn)
        row = conn.execute("SELECT * FROM media_targets WHERE singleton=1").fetchone()
    if row is None:
        return {"enabled": False, "revision": 0, "movie": None, "series": None}
    return {
        "enabled": True,
        "revision": row["revision"],
        "movie": row["movie"],
        "series": row["series"],
        "movie_root": row["movie_root"],
        "series_root": row["series_root"],
    }


def root(path_id):
    if not isinstance(path_id, int) or isinstance(path_id, bool) or path_id <= 0:
        raise ValueError("Select an existing custom download path")
    entry = db.get_custom_path(path_id)
    if entry is None:
        raise ValueError("The configured media mount no longer exists")
    value = paths.expand(entry["path"]).resolve()
    if not value.is_dir():
        raise ValueError("The configured media mount is not available")
    return value


def save(movie, series, revision):
    first, second = root(movie), root(series)
    if first == second or first in second.parents or second in first.parents:
        raise ValueError(
            "Movie and show destinations must be separate, non-overlapping folders"
        )
    for folder in (first, second):
        probe = folder / (".media-target-" + uuid.uuid4().hex)
        try:
            with probe.open("x", encoding="utf-8") as output:
                output.write("target-check")
        finally:
            probe.unlink(missing_ok=True)
    with db.session() as conn:
        _schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute(
            "SELECT revision FROM media_targets WHERE singleton=1"
        ).fetchone()
        current = old[0] if old else 0
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision != current
        ):
            raise ValueError("Media destinations changed; reload before saving")
        conn.execute(
            "INSERT OR REPLACE INTO media_targets(singleton,movie,series,revision,movie_root,series_root) VALUES (1,?,?,?,?,?)",
            (movie, series, current + 1, str(first), str(second)),
        )
    return read()


def resolve(kind, selected=None, expected_root=None, revision=None):
    config = read()
    if not config["enabled"]:
        return None
    if kind not in ("movie", "series"):
        raise ValueError(
            "Verify whether this title is a movie or a show before downloading"
        )
    target = config[kind]
    folder = root(target)
    if str(folder) != config.get(kind + "_root"):
        raise ValueError(
            "A media mount changed; verify and save the destinations again"
        )
    # Detect two mounts changed to the same physical folder after setup as well.
    other = root(config["series" if kind == "movie" else "movie"])
    if folder == other or folder in other.parents or other in folder.parents:
        raise ValueError("Movie and show mounts now overlap; review media destinations")
    if selected is not None and selected != target:
        raise ValueError(
            "The selected path conflicts with the detected movie/show destination"
        )
    if expected_root is not None and os.path.normcase(str(folder)) != os.path.normcase(
        str(expected_root)
    ):
        raise ValueError(
            "The queued media mount changed; review and submit the title again"
        )
    if revision is not None and revision != config["revision"]:
        raise ValueError("Media destinations changed after this download was queued")
    return target, folder, config["revision"]
