"""Small, fixed-origin TMDB client. Credentials never leave this module."""

import os
import re
import time
from datetime import UTC, datetime

import niquests as requests

from .settings_store import TMDB_KEY

BASE_URL = "https://api.themoviedb.org/3"


class TMDBError(RuntimeError):
    pass


class TMDBTransientError(TMDBError):
    """A temporary transport, rate-limit or upstream-server failure."""


def configured():
    return bool(os.environ.get(TMDB_KEY, "").strip())


def _get(path, **params):
    token = os.environ.get(TMDB_KEY, "").strip()
    if not token:
        raise TMDBError("TMDB is not configured")
    headers = {"Accept": "application/json", "User-Agent": "H0melab Downloader"}
    if token.startswith("eyJ") or len(token) > 64:
        headers["Authorization"] = f"Bearer {token}"
    else:
        params["api_key"] = token
    for attempt in range(3):
        try:
            response = requests.get(
                f"{BASE_URL}/{path.lstrip('/')}", params=params,
                headers=headers, timeout=15, allow_redirects=False,
            )
        except Exception:
            if attempt < 2:
                time.sleep(0.2 * (2 ** attempt))
                continue
            raise TMDBTransientError("TMDB request failed") from None
        if response.status_code in (401, 403):
            raise TMDBError("TMDB rejected the configured key")
        if response.status_code == 429 or 500 <= response.status_code < 600:
            if attempt < 2:
                time.sleep(0.2 * (2 ** attempt))
                continue
        if response.status_code == 429 or 500 <= response.status_code < 600:
            raise TMDBTransientError(f"TMDB request failed ({response.status_code})")
        if not 200 <= response.status_code < 300:
            raise TMDBError(f"TMDB request failed ({response.status_code})")
        try:
            data = response.json()
        except (ValueError, TypeError):
            raise TMDBError("TMDB returned an invalid response") from None
        if not isinstance(data, dict):
            raise TMDBError("TMDB returned an invalid response")
        return data



def _movie(item):
    release = item.get("release_date") or ""
    return {
        "tmdb_id": int(item["id"]),
        "title": item.get("title") or item.get("original_title") or "Unknown",
        "original_title": item.get("original_title") or "",
        "release_date": release,
        "release_year": int(release[:4])
        if len(release) >= 4 and release[:4].isdigit()
        else 0,
        "poster_path": item.get("poster_path"),
        "overview": item.get("overview") or "",
        "media_type": "movie",
    }


def _series(item):
    release = item.get("first_air_date") or ""
    return {
        "tmdb_id": int(item["id"]),
        "title": item.get("name") or item.get("original_name") or "Unknown",
        "original_title": item.get("original_name") or "",
        "release_date": release,
        "release_year": int(release[:4])
        if len(release) >= 4 and release[:4].isdigit()
        else 0,
        "poster_path": item.get("poster_path"),
        "overview": item.get("overview") or "",
        "media_type": "tv",
    }


def search_movies(query, language="de-DE"):
    data = _get("search/movie", query=query, language=language, include_adult="false")
    return [_movie(item) for item in data.get("results", []) if item.get("id")]


def search_media(query, media_type="multi", language="de-DE"):
    if media_type not in ("movie", "tv", "multi"):
        raise TMDBError("Invalid media type")
    path = "search/multi" if media_type == "multi" else f"search/{media_type}"
    data = _get(path, query=query, language=language, include_adult="false")
    results = []
    for item in data.get("results", []):
        kind = item.get("media_type") or media_type
        if not item.get("id") or kind not in ("movie", "tv"):
            continue
        results.append(_movie(item) if kind == "movie" else _series(item))
    return results


def upcoming_movies(language="de-DE"):
    data = _get("movie/upcoming", language=language, region="DE")
    today = datetime.now(UTC).date().isoformat()
    return [
        _movie(item)
        for item in data.get("results", [])
        if (item.get("release_date") or "") >= today
    ]


def movie_details(tmdb_id, language="de-DE"):
    if type(tmdb_id) is not int or tmdb_id <= 0:
        raise TMDBError("Invalid TMDB movie id")
    return _movie(_get(f"movie/{tmdb_id}", language=language))


def media_details(tmdb_id, media_type="movie", language="de-DE"):
    if type(tmdb_id) is not int or tmdb_id <= 0:
        raise TMDBError("Invalid TMDB id")
    if media_type == "movie":
        return _movie(_get(f"movie/{tmdb_id}", language=language))
    if media_type == "tv":
        return _series(_get(f"tv/{tmdb_id}", language=language))
    raise TMDBError("Invalid media type")


def _positive_id(value):
    if type(value) is not int or value <= 0:
        raise TMDBError("Invalid TMDB id")
    return value


def _imdb_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"tt[0-9]{7,12}", value):
        raise TMDBError("Invalid IMDb id")
    return value


def identity_details(media_type, tmdb_id=None, imdb_id=None, language="de-DE"):
    """Resolve a typed identity; never associate an unverified external ID."""
    if media_type not in ("movie", "tv"):
        raise TMDBError("Invalid media type")
    if tmdb_id is not None:
        _positive_id(tmdb_id)
    if imdb_id is not None:
        _imdb_id(imdb_id)
    if tmdb_id is None:
        if imdb_id is None:
            raise TMDBError("A TMDB or IMDb id is required")
        result = _get(f"find/{imdb_id}", external_source="imdb_id", language=language)
        rows = result.get("movie_results" if media_type == "movie" else "tv_results")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise TMDBError("IMDb identity is not uniquely resolved for this media type")
        tmdb_id = _positive_id(rows[0].get("id"))
    raw = _get(f"{media_type}/{tmdb_id}", language=language)
    if type(raw.get("id")) is not int or raw["id"] != tmdb_id:
        raise TMDBError("TMDB returned a conflicting identity")
    title_key = "title" if media_type == "movie" else "name"
    if not isinstance(raw.get(title_key), str) or not raw[title_key].strip():
        raise TMDBError("TMDB returned incomplete identity metadata")
    external = _get(f"{media_type}/{tmdb_id}/external_ids")
    if external.get("id") != tmdb_id or type(external.get("id")) is not int:
        raise TMDBError("TMDB external IDs belong to a different identity")
    resolved_imdb = external.get("imdb_id") or None
    if resolved_imdb is not None:
        _imdb_id(resolved_imdb)
    if imdb_id is not None and resolved_imdb != imdb_id:
        raise TMDBError("IMDb id does not match the TMDB identity")
    if raw.get("imdb_id") and raw["imdb_id"] != resolved_imdb:
        raise TMDBError("TMDB returned conflicting IMDb IDs")
    alternatives = _get(f"{media_type}/{tmdb_id}/alternative_titles")
    if alternatives.get("id") != tmdb_id or type(alternatives.get("id")) is not int:
        raise TMDBError("Alternative titles belong to a different identity")
    rows = alternatives.get("titles" if media_type == "movie" else "results", [])
    if not isinstance(rows, list):
        raise TMDBError("TMDB returned invalid alternative titles")
    result = _movie(raw) if media_type == "movie" else _series(raw)
    result.update(imdb_id=resolved_imdb, external_ids=external,
                  alternative_titles=list(dict.fromkeys(
                      row["title"] for row in rows if isinstance(row, dict)
                      and isinstance(row.get("title"), str) and row["title"].strip())),
                  seasons=[])
    if media_type == "tv":
        seasons = raw.get("seasons", [])
        if not isinstance(seasons, list) or any(
            not isinstance(row, dict) or type(row.get("season_number")) is not int
            or row["season_number"] < 0 for row in seasons
        ):
            raise TMDBError("TMDB returned invalid seasons")
        if len({row["season_number"] for row in seasons}) != len(seasons):
            raise TMDBError("TMDB returned duplicate seasons")
        result["seasons"] = seasons
    return result


def season_details(tmdb_id, season_number, language="de-DE"):
    """Fetch a season with validated numbering and explicit parent identity."""
    _positive_id(tmdb_id)
    if type(season_number) is not int or season_number < 0:
        raise TMDBError("Invalid season number")
    result = _get(f"tv/{tmdb_id}/season/{season_number}", language=language)
    if type(result.get("season_number")) is not int or result["season_number"] != season_number:
        raise TMDBError("TMDB returned a conflicting season")
    episodes = result.get("episodes")
    if not isinstance(episodes, list):
        raise TMDBError("TMDB returned invalid episodes")
    seen = set()
    for row in episodes:
        if not isinstance(row, dict):
            raise TMDBError("TMDB returned invalid episodes")
        number = row.get("episode_number")
        if (type(number) is not int or number <= 0 or number in seen
                or type(row.get("season_number")) is not int
                or row["season_number"] != season_number
                or (row.get("show_id") is not None and row["show_id"] != tmdb_id)):
            raise TMDBError("TMDB returned conflicting episode numbering")
        seen.add(number)
        row["series_tmdb_id"] = tmdb_id
    return {**result, "series_tmdb_id": tmdb_id}
