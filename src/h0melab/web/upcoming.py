"""Availability watchlist checker with deliberately strict title matching."""

import re
import threading
import time
import unicodedata
from datetime import UTC, datetime

from ..logger import get_logger
from . import db, media, settings_store, sitesearch, tmdb
from .media import WORKING_PROVIDERS

logger = get_logger(__name__)
_started = False
_start_lock = threading.Lock()
_run_lock = threading.Lock()


def _normalise(value):
    value = unicodedata.normalize("NFKD", value or "").casefold()
    return re.sub(r"[^a-z0-9]+", "", value)


def _release_year(found):
    value = str(getattr(found, "release_year", "") or "")
    match = re.search(r"(?:19|20)\d{2}", value)
    return match.group(0) if match else ""


def _exact_hit(item):
    """Return only a TMDB-id hit or an exact title plus exact year hit."""
    titles = list(
        dict.fromkeys(filter(None, (item["title"], item.get("original_title", ""))))
    )
    aliases = {_normalise(value) for value in titles} - {""}
    media_type = item.get("media_type", "movie")
    sites = sitesearch.SERIES_SITES if media_type == "tv" else sitesearch.MOVIE_SITES
    candidates = []

    for site in sites:
        if not settings_store.site_enabled(site):
            continue
        for readable in titles:
            for hit in sitesearch.search(site, readable):
                if _normalise(hit.get("title")) not in aliases:
                    continue
                try:
                    from ..providers import resolve_provider

                    provider = resolve_provider(hit["url"])
                    found = provider.series_cls(url=hit["url"])
                    year = _release_year(found)
                except Exception:
                    year = ""
                expected = str(item.get("release_year") or "")
                if not expected or year != expected:
                    continue
                candidates.append(hit)

    unique = {hit["url"]: hit for hit in candidates}
    return next(iter(unique.values()), None)


def _usable_provider(url, language, preferred):
    from ..providers import resolve_provider

    resolved = resolve_provider(url)
    episode = resolved.episode_cls(url=url, selected_language=language)
    available = media.provider_map(episode.provider_data).get(language, [])
    if preferred in available:
        return preferred
    return available[0] if available else None


def _series_episodes(url, language):
    """Enumerate currently available, not-yet-downloaded episodes for one hit."""
    from ..providers import resolve_provider
    from .views import api_media

    provider = resolve_provider(url)
    if provider.name in api_media.SINGLE_PAGE_SITES:
        rows = api_media._single_page_episodes(provider, url)
    else:
        found = provider.series_cls(url=url)
        rows = []
        for season in found.seasons:
            rows.extend(api_media._season_episodes(provider, season.url, url))
    return [
        row["url"]
        for row in rows
        if not row.get("downloaded") and language in row.get("available_languages", ())
    ]


def _queue_item(item, hit):
    preferred = (
        item["provider"]
        if item["provider"] in WORKING_PROVIDERS
        else WORKING_PROVIDERS[0]
    )
    if item.get("media_type", "movie") == "tv":
        episodes = _series_episodes(hit["url"], item["language"])
        if not episodes:
            return None, "Exact series found, but no new episodes are available in the selected language"
        provider = _usable_provider(episodes[0], item["language"], preferred)
    else:
        episodes = [hit["url"]]
        provider = _usable_provider(hit["url"], item["language"], preferred)
    if not provider:
        return None, "Exact title found, but no usable provider exists for the selected language"
    queue_id = db.add_to_queue(
        item["title"], hit["url"], episodes, item["language"], provider,
        custom_path_id=item["custom_path_id"], source="upcoming",
    )
    return queue_id, "Unique exact match queued"


def run_cycle():
    if not tmdb.configured() or not _run_lock.acquire(blocking=False):
        return False
    try:
        now_dt = datetime.now(UTC)
        now = now_dt.replace(microsecond=0).isoformat()
        today = now_dt.date().isoformat()
        for item in db.list_upcoming_movies():
            if item["status"] in ("paused", "queued", "downloaded"):
                continue
            if item["release_date"] and item["release_date"] > today:
                continue
            try:
                hit = _exact_hit(item)
                if not hit:
                    db.update_upcoming_movie(
                        item["id"], status="waiting", last_checked_at=now,
                        last_message="No unique exact match yet",
                    )
                    continue
                queue_id, message = _queue_item(item, hit)
                db.update_upcoming_movie(
                    item["id"], status="queued" if queue_id else "waiting",
                    matched_url=hit["url"], queue_id=queue_id,
                    last_checked_at=now, last_message=message,
                )
            except Exception as exc:
                logger.warning("Upcoming check failed for item %s: %s", item["id"], exc)
                db.update_upcoming_movie(
                    item["id"], status="error", last_checked_at=now,
                    last_message=str(exc)[:500],
                )
        db.upcoming_state_set("last_run", now)
        db.upcoming_state_set(
            "next_run",
            (now_dt + settings_store.upcoming_interval()).replace(microsecond=0).isoformat(),
        )
        return True
    finally:
        _run_lock.release()


def run_async():
    threading.Thread(target=run_cycle, name="h0melab-upcoming-manual", daemon=True).start()


def next_run_at():
    last = db.upcoming_state_get("last_run")
    if not last:
        return db.upcoming_state_get("next_run")
    try:
        parsed = datetime.fromisoformat(last)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return (parsed + settings_store.upcoming_interval()).replace(microsecond=0).isoformat()
    except (TypeError, ValueError):
        return db.upcoming_state_get("next_run")


def ensure_started():
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    def loop():
        while True:
            try:
                last = db.upcoming_state_get("last_run")
                due = True
                if last:
                    try:
                        parsed = datetime.fromisoformat(last)
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=UTC)
                        due = datetime.now(UTC) - parsed >= settings_store.upcoming_interval()
                    except (TypeError, ValueError):
                        pass
                if due:
                    run_cycle()
            except Exception:
                logger.exception("Upcoming scheduler cycle failed")
            time.sleep(60)

    threading.Thread(target=loop, name="h0melab-upcoming", daemon=True).start()
