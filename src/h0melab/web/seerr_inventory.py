"""Conservative import of locally identified Jellyfin NFO media, never filename guesses."""

import os
from pathlib import Path
from xml.etree import ElementTree

from . import db, media_files, seerr, seerr_files

VIDEO_SUFFIXES = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".webm"}
MAX_NFO_BYTES = 1024 * 1024
MAX_FILES = 20000


class _NoDoctype(ElementTree.TreeBuilder):
    def doctype(self, name, pubid, system):
        raise ValueError("NFO document types are not accepted")


def read_nfo(root, path):
    try:
        path = media_files.checked_path(root, path)
        with path.open("rb") as stream:
            raw = stream.read(MAX_NFO_BYTES + 1)
        if (
            len(raw) > MAX_NFO_BYTES
            or b"<!DOCTYPE" in raw.upper()
            or b"<!ENTITY" in raw.upper()
        ):
            return None
        return ElementTree.fromstring(
            raw, parser=ElementTree.XMLParser(target=_NoDoctype())
        )
    except (OSError, ValueError, ElementTree.ParseError):
        return None


def matches(document, identity, tag):
    if document is None or document.tag != tag:
        return False
    values = {"tmdb": set(), "imdb": set()}
    for element in document:
        kind = {"tmdbid": "tmdb", "imdbid": "imdb", "imdb_id": "imdb"}.get(element.tag)
        if element.tag == "uniqueid":
            kind = element.get("type", "").lower()
        if kind in values and element.text and element.text.strip():
            values[kind].add(element.text.strip())
    confirmed = False
    for kind, field in (("tmdb", "tmdb_id"), ("imdb", "imdb_id")):
        if not values[kind]:
            continue
        expected = identity.get(field)
        if expected is None or values[kind] != {str(expected)}:
            return False
        confirmed = True
    return confirmed


def discover(root, identity, requested_seasons=()):
    """Scan only the selected library mount, with bounded reads and no linked paths."""
    root = Path(root)
    documents = []
    count = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        base = Path(directory)
        dirs[:] = [
            name
            for name in dirs
            if not (base / name).is_symlink()
            and not (
                hasattr(base / name, "is_junction") and (base / name).is_junction()
            )
        ]
        count += len(names) + len(dirs)
        if count > MAX_FILES:
            raise ValueError(
                "Existing library scan exceeds its bound; review library size before retrying"
            )
        documents.extend(base / name for name in names if name.lower().endswith(".nfo"))
    found = []
    shows = {}
    if identity["media_type"] == "tv":
        shows = {
            path.parent: read_nfo(root, path)
            for path in documents
            if path.name.lower() == "tvshow.nfo"
        }
    for path in documents:
        document = read_nfo(root, path)
        if identity["media_type"] == "movie":
            if not matches(document, identity, "movie"):
                continue
            season, number = 0, 0
        else:
            if document is None or document.tag != "episodedetails":
                continue
            parent = next(
                (directory for directory in path.parents if directory in shows), None
            )
            if parent is None or not matches(shows[parent], identity, "tvshow"):
                continue
            seasons, episodes = document.findall("season"), document.findall("episode")
            if len(seasons) != 1 or len(episodes) != 1:
                continue
            try:
                season, number = int(seasons[0].text), int(episodes[0].text)
            except (TypeError, ValueError):
                continue
            if season not in requested_seasons or number < 1:
                continue
        candidates = [
            path.with_suffix(ext)
            for ext in VIDEO_SUFFIXES
            if path.with_suffix(ext).is_file()
        ]
        if identity["media_type"] == "movie" and path.name.lower() == "movie.nfo":
            candidates = [
                p
                for p in path.parent.iterdir()
                if p.suffix.lower() in VIDEO_SUFFIXES and p.is_file()
            ]
        if len(candidates) != 1:
            continue
        try:
            video = media_files.checked_path(root, candidates[0])
            if video.stat().st_size <= 0:
                continue
        except (ValueError, OSError):
            continue
        entry = {"_seerr_identity": identity, "season": season, "episode": number}
        found.append((entry, video))
    # An identified existing show owns its one library-level folder. Never create
    # a second title-derived folder alongside it, or choose among duplicate shows.
    identified = [
        directory
        for directory, doc in shows.items()
        if matches(doc, identity, "tvshow")
    ]
    if identified:
        relatives = [directory.relative_to(root) for directory in identified]
        if len(relatives) != 1 or len(relatives[0].parts) != 1:
            raise ValueError("Existing series folders require manual consolidation")
        name = relatives[0].name
        with db.session() as conn:
            seerr.schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT folder FROM seerr_media WHERE media_type='tv' AND tmdb_id=?",
                (identity["tmdb_id"],),
            ).fetchone()
            collision = conn.execute(
                "SELECT tmdb_id FROM seerr_media WHERE media_type='tv' AND folder=?",
                (name,),
            ).fetchone()
            if (existing and existing[0] != name) or (
                collision and collision[0] != identity["tmdb_id"]
            ):
                raise ValueError(
                    "Existing series folder conflicts with its identity binding"
                )
            if not existing:
                conn.execute(
                    "INSERT INTO seerr_media VALUES (?,?,?,?)",
                    ("tv", identity["tmdb_id"], identity.get("imdb_id"), name),
                )
    # Persist only after the entire bounded inventory completed successfully.
    for entry, video in found:
        with media_files.file_operation(video):
            seerr_files.record(entry, media_files.checked_path(root, video))
    return len(found)
