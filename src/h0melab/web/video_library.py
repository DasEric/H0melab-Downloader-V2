"""Source-scoped episode families, durable real-URL anchors and video paths.

Anchors and season links are real provider URLs, never synthetic provider pages.
Only server-observed search/detail data enters this registry. A group ID survives
cache expiry, title refresh, result order changes and restarts.
"""

import hashlib
import json
import re
import threading
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

from ..models.common.library_layout import safe_component
from . import db

FLAT_SOURCES = {"filmpalast", "filmo"}
MAX_ITEMS = 20_000
_locks = [threading.Lock() for _ in range(64)]
_EPISODE = re.compile(
    r"^(?P<title>.+?)[ ._-]*(?:s(?P<s>\d{1,3})[ ._-]*e(?P<e>\d{1,4})(?:[ ._-]*-?[ ._-]*e(?P<end>\d{1,4}))?|(?P<xs>\d{1,3})x(?P<xe>\d{1,4}))(?:\b|[ ._-]|$)",
    re.IGNORECASE,
)


def parse_title(title):
    match = _EPISODE.match(str(title or ""))
    if not match:
        return None
    name = re.sub(r"[._]+", " ", match["title"]).strip(" -_")
    year = re.search(r"\s*\(((?:19|20)\d{2})\)$", name)
    if year:
        name = name[: year.start()].strip()
    season, episode = int(match["s"] or match["xs"]), int(match["e"] or match["xe"])
    end = int(match["end"] or episode)
    if not name or season > 999 or episode < 1 or end < episode or end - episode > 100:
        raise ValueError("Invalid season/episode numbering")
    return {
        "title": name,
        "year": year[1] if year else "",
        "season": season,
        "episode": episode,
        "end": end,
        "media_type": "series",
    }


def _schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS video_titles (
        id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
        year TEXT NOT NULL, anchor TEXT NOT NULL UNIQUE,
        complete INTEGER NOT NULL DEFAULT 0, refreshed REAL NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS video_episodes (
        url TEXT PRIMARY KEY, title_id TEXT NOT NULL, season INTEGER NOT NULL,
        episode INTEGER NOT NULL, end_episode INTEGER NOT NULL)""")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS video_title_members ON video_episodes(title_id)"
    )


def _key(source, identity):
    name = unicodedata.normalize("NFKC", identity["title"]).casefold()
    name = re.sub(r"\s+", " ", name).strip()
    parent = identity.get("parent")
    return hashlib.sha256(
        (
            source
            + "\n"
            + ("parent:" + parent if parent else name + "\n" + identity["year"])
        ).encode()
    ).hexdigest()


def parse_item(item):
    parsed = parse_title(item.get("title") or item.get("name"))
    if (
        isinstance(item.get("series_title"), str)
        and isinstance(item.get("season_number"), int)
        and isinstance(item.get("episode_number"), int)
    ):
        if (
            not item["series_title"].strip()
            or not 0 <= item["season_number"] <= 999
            or not 1 <= item["episode_number"] <= 10000
        ):
            raise ValueError("Invalid structured episode identity")
        parsed = {
            "title": item["series_title"],
            "year": str(item.get("series_year") or ""),
            "season": item["season_number"],
            "episode": item["episode_number"],
            "end": item["episode_number"],
            "media_type": "series",
        }
    if parsed:
        parsed["parent"] = str(item.get("series_id") or item.get("series_url") or "")
    return parsed


def by_url(url):
    with db.session() as conn:
        _schema(conn)
        row = conn.execute(
            """SELECT t.*, e.url, e.season, e.episode, e.end_episode
            FROM video_episodes e JOIN video_titles t ON t.id=e.title_id WHERE e.url=?""",
            (url,),
        ).fetchone()
    return dict(row) if row else None


def members(identity):
    with db.session() as conn:
        _schema(conn)
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM video_episodes WHERE title_id=? ORDER BY season,episode,url",
                (identity["id"],),
            )
        ]


def group_results(source, items):
    if source not in FLAT_SOURCES:
        return items
    if len(items) > MAX_ITEMS:
        raise ValueError("Too many source results; refine the search")
    output, seen = [], set()
    for item in items:
        parsed = parse_item(item)
        url = item.get("url")
        if not parsed or not isinstance(url, str):
            output.append(item)
            continue
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        expected = "filmpalast.to" if source == "filmpalast" else "filmo.to"
        if host != expected or urlparse(url).scheme not in ("http", "https"):
            raise ValueError("Episode URL does not belong to the selected source")
        key = _key(source, parsed)
        existing = by_url(url)
        if (
            existing
            and not parsed.get("parent")
            and existing["title"].casefold() == parsed["title"].casefold()
            and existing["year"] == parsed["year"]
        ):
            key = existing["id"]
        with db.session() as conn:
            _schema(conn)
            conn.execute(
                "INSERT OR IGNORE INTO video_titles(id,source,title,year,anchor) VALUES (?,?,?,?,?)",
                (key, source, parsed["title"], parsed["year"], url),
            )
            old = conn.execute(
                "SELECT title_id FROM video_episodes WHERE url=?", (url,)
            ).fetchone()
            if old and old[0] != key:
                raise ValueError("The provider changed this episode's series identity")
            conn.execute(
                "INSERT OR REPLACE INTO video_episodes VALUES (?,?,?,?,?)",
                (url, key, parsed["season"], parsed["episode"], parsed["end"]),
            )
            group = dict(
                conn.execute("SELECT * FROM video_titles WHERE id=?", (key,)).fetchone()
            )
        if key not in seen:
            seen.add(key)
            output.append(
                {
                    **item,
                    "title": group["title"],
                    "url": group["anchor"],
                    "media_type": "series",
                    "video_id": key,
                    "grouped_series": True,
                    "year": group["year"],
                    "complete": bool(
                        group["complete"] and time.time() - group["refreshed"] < 60
                    ),
                }
            )
    return output


def resolve_group(url):
    identity = by_url(url)
    if identity is None:
        return None
    gate = _locks[int(identity["id"][:8], 16) % len(_locks)]
    with gate:
        identity = by_url(url)
        if identity["complete"] and time.time() - identity["refreshed"] < 60:
            return identity
        from .. import search

        query = (
            search.query_filmpalast
            if identity["source"] == "filmpalast"
            else search.query_filmo
        )
        hits = query(identity["title"], complete=True)
        if not getattr(hits, "complete", False):
            raise ValueError(
                "The source's episode list is incomplete; please retry before downloading"
            )
        # Resolve a fresh set, not a union with now-removed episodes. Keep the
        # durable URL identities for old receipts, but only current members are
        # exposed through the separate active list in title detail responses.
        group_results(identity["source"], hits)
        active = [
            item["url"]
            for item in hits
            if (known := by_url(item.get("url"))) and known["id"] == identity["id"]
        ]
        if not active or len(active) > MAX_ITEMS:
            raise ValueError("The source did not confirm a bounded episode list")
        with db.session() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS video_active_members(title_id TEXT PRIMARY KEY, urls TEXT NOT NULL)"""
            )
            conn.execute(
                "INSERT OR REPLACE INTO video_active_members VALUES (?,?)",
                (identity["id"], json.dumps(active)),
            )
            conn.execute(
                "UPDATE video_titles SET complete=1,refreshed=? WHERE id=?",
                (time.time(), identity["id"]),
            )
        return by_url(url)


def active_members(identity):
    with db.session() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS video_active_members(title_id TEXT PRIMARY KEY, urls TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT urls FROM video_active_members WHERE title_id=?", (identity["id"],)
        ).fetchone()
    active = set(json.loads(row[0])) if row else set()
    rows = [member for member in members(identity) if member["url"] in active]
    unique = {}
    for member in rows:
        unique.setdefault(
            (member["season"], member["episode"], member["end_episode"]), member
        )
    return list(unique.values())


def variant_offers(url):
    identity = by_url(url)
    if identity is None:
        return []
    from .views import api_media

    rows = [
        row
        for row in members(identity)
        if (row["season"], row["episode"], row["end_episode"])
        == (identity["season"], identity["episode"], identity["end_episode"])
    ]
    with db.session() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS video_active_members(title_id TEXT PRIMARY KEY, urls TEXT NOT NULL)"
        )
        active = conn.execute(
            "SELECT urls FROM video_active_members WHERE title_id=?", (identity["id"],)
        ).fetchone()
    if active:
        allowed = set(json.loads(active[0]))
        rows = [row for row in rows if row["url"] in allowed]
    if len(rows) > 64:
        raise ValueError("Too many variants for one episode")
    output = []
    for row in rows:
        try:
            provider = api_media.resolve_provider(row["url"])
            episode = provider.episode_cls(
                url=row["url"], **api_media._build_kwargs(provider)
            )
            output.append(
                (
                    row["url"],
                    api_media.media.provider_map(
                        episode.provider_data,
                        drop_english_sub=api_media.english_sub_disabled(),
                    ),
                )
            )
        except Exception:
            api_media.logger.warning("One episode variant could not be verified")
    if not output:
        raise ValueError("No current episode variant could be verified")
    return output


def variant_url(url, language, hoster):
    if by_url(url) is None:
        return url
    for candidate, offers in variant_offers(url):
        available = offers.get(language) or []
        if available and (hoster == "Automatic" or hoster in available):
            return candidate
    raise ValueError("The selected episode language/provider is no longer available")


def identity_for_episode(episode, url):
    source_identity = getattr(episode, "video_identity", None)
    if isinstance(source_identity, dict):
        return source_identity
    known = by_url(url)
    if known:
        parsed = parse_title(getattr(episode, "title", ""))
        if parsed and (
            parsed["title"].casefold() != known["title"].casefold()
            or parsed["year"] != known["year"]
            or parsed["season"] != known["season"]
            or parsed["episode"] != known["episode"]
            or parsed["end"] != known["end_episode"]
        ):
            raise ValueError("The episode changed since selection; reopen this title")
        return {**known, "media_type": "series", "end": known["end_episode"]}
    title = (
        getattr(episode, "title", "")
        or getattr(episode, "title_de", "")
        or getattr(episode, "title_en", "")
    )
    parsed = parse_title(title)
    if parsed:
        return parsed
    season = getattr(episode, "season", None)
    series = getattr(season, "series", None) if season is not None else None
    movie = getattr(episode, "is_movie", None)
    if movie is True:
        return {
            "media_type": "movie",
            "title": title,
            "year": str(getattr(episode, "release_year", "") or ""),
        }
    season_number = getattr(episode, "season_number", None)
    if season_number is None and season is not None:
        season_number = getattr(season, "season_number", None)
    episode_number = getattr(episode, "episode_number", None)
    if (
        isinstance(season_number, int)
        and isinstance(episode_number, int)
        and season_number >= 0
        and episode_number > 0
    ):
        return {
            "media_type": "series",
            "title": getattr(series, "title", "") or title,
            "year": str(getattr(series, "release_year", "") or ""),
            "season": season_number,
            "episode": episode_number,
            "end": episode_number,
        }
    if getattr(episode, "is_series", None) is False:
        return {
            "media_type": "movie",
            "title": title,
            "year": str(getattr(episode, "release_year", "") or ""),
        }
    return None


def remember(url, identity):
    with db.session() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS video_observations(url TEXT PRIMARY KEY, identity TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO video_observations VALUES (?,?)",
            (url, json.dumps(identity)),
        )


def observed(url):
    known = by_url(url)
    if known:
        return {**known, "media_type": "series", "end": known["end_episode"]}
    with db.session() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS video_observations(url TEXT PRIMARY KEY, identity TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT identity FROM video_observations WHERE url=?", (url,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def inspect(url, language="German Dub"):
    cached = observed(url)
    if cached:
        return cached
    from .views import api_media

    provider = api_media.resolve_provider(url)
    episode = provider.episode_cls(url=url, selected_language=language)
    source = {"FilmPalast": "filmpalast", "Filmo": "filmo"}.get(provider.name)
    if source and parse_title(getattr(episode, "title", "")):
        group_results(source, [{"title": episode.title, "url": url}])
    result = identity_for_episode(episode, url)
    if result is None:
        raise ValueError(
            "The source did not verify this title's type and episode numbering"
        )
    remember(url, result)
    return result


def output_path(root, identity):
    title = safe_component(identity["title"], "", max_length=100)
    if not title:
        raise ValueError("A verified title is required for the media path")
    year = str(identity.get("year") or "")
    name = f"{title} ({year})" if re.fullmatch(r"(?:19|20)\d{2}", year) else title
    if identity["media_type"] == "movie":
        return Path(root) / name / (name + ".mkv")
    season, episode = identity["season"], identity["episode"]
    number = f"S{season:02d}E{episode:02d}"
    if identity.get("end", episode) != episode:
        number += f"-E{identity['end']:02d}"
    return Path(root) / name / f"Season {season:02d}" / f"{name} - {number}.mkv"


def reserve_output(path, identity):
    import os

    name = os.path.normcase(str(Path(path).absolute()))
    key = json.dumps(
        {
            field: identity.get(field)
            for field in (
                "media_type",
                "title",
                "year",
                "season",
                "episode",
                "end",
                "id",
            )
        },
        sort_keys=True,
    )
    with db.session() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS video_outputs(path TEXT PRIMARY KEY, identity TEXT NOT NULL)"
        )
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT identity FROM video_outputs WHERE path=?", (name,)
        ).fetchone()
        if existing and existing[0] != key:
            raise ValueError(
                "Two different titles map to the same filename; review this media identity"
            )
        conn.execute("INSERT OR IGNORE INTO video_outputs VALUES (?,?)", (name, key))
