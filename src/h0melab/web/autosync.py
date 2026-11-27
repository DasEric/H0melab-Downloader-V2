"""Keep an explicit list of AniWorld and SerienStream series up to date.

Each list entry is one concrete copy: series, language and destination. A run
checks only those entries and compares the language-specific episodes offered
by the source with the files already present at that destination.
"""

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse, urlunparse

from ..config import LANG_CODE_MAP, LANG_KEY_MAP, LANG_LABELS
from ..logger import get_logger
from ..providers import normalize_url, resolve_provider
from . import db, paths, schedule
from .media import WORKING_PROVIDERS, folder_matches_title
from .settings_store import (
    autosync_cron_schedule,
    autosync_enabled,
    autosync_interval_seconds,
    autosync_mode,
    autosync_new_only,
    autosync_schedule_description,
)

logger = get_logger(__name__)

TICK_SECONDS = 300
MIN_TICK_SECONDS = 5
_CLOCK_SLACK = timedelta(minutes=5)

SITE_LANGUAGES = {
    "aniworld": ("German Dub", "German Sub", "English Dub", "English Sub"),
    "sto": ("German Dub", "English Dub"),
}

_run_lock = threading.Lock()
_started = False
_start_lock = threading.Lock()
_anchored_at = None


class DuplicateSubscription(ValueError):
    """The same series/language/destination is already tracked."""


def _now():
    return datetime.now(UTC)


def _local(moment):
    """A UTC instant as naive local wall-clock time, which is what cron means."""
    return moment.astimezone().replace(tzinfo=None)


def _utc(wall_clock):
    """Naive local wall-clock time back to UTC."""
    return wall_clock.astimezone(UTC)


def _parse(value):
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _label_lookup():
    lookup = {}
    for key, (audio, subtitles) in LANG_KEY_MAP.items():
        label = LANG_LABELS.get(key)
        if label:
            lookup[(LANG_CODE_MAP[audio], LANG_CODE_MAP[subtitles])] = label
    return lookup


def languages_from_probe(episode_path):
    """Language labels a downloaded file actually contains."""
    from ..models.common.common import check_downloaded

    try:
        probe = check_downloaded(episode_path)
    except Exception as exc:
        logger.debug("AutoSync: probe failed for %s: %s", episode_path, exc)
        return set()
    if not probe.get("exists"):
        return set()

    audio_langs = probe.get("audio_langs") or set()
    video_langs = probe.get("video_langs") or set()
    found = set()
    for (audio_code, sub_code), label in _label_lookup().items():
        if audio_code in audio_langs and (sub_code is None or sub_code in video_langs):
            found.add(label)
    return found


def subtitle_languages_from_probe(episode_path):
    """Canonical subtitle language codes present in one downloaded file."""
    from ..models.common.common import check_downloaded
    from ..models.common.subtitles import normalize_subtitle_language

    try:
        probe = check_downloaded(episode_path)
    except Exception as exc:
        logger.debug("AutoSync: subtitle probe failed for %s: %s", episode_path, exc)
        return set()
    if not probe.get("exists"):
        return set()
    return {
        normalize_subtitle_language(language)
        for language in (probe.get("subtitle_langs") or set())
    }


def canonical_series_url(url):
    """Validate a series URL and store it under one stable canonical host."""
    if not isinstance(url, str) or not url.strip():
        raise TypeError("series_url must be a non-empty string.")
    normalized = normalize_url(url.strip())
    provider = resolve_provider(normalized)
    if provider.name not in {"AniWorld", "SerienStream"}:
        raise ValueError("Auto-Sync supports only AniWorld and SerienStream series.")
    if not provider.series_pattern or not provider.series_pattern.fullmatch(normalized):
        raise ValueError("Please select a series page, not a season or episode.")

    parsed = urlparse(normalized)
    host = "aniworld.to" if provider.name == "AniWorld" else "serienstream.to"
    canonical = urlunparse(("https", host, parsed.path.rstrip("/"), "", "", ""))
    return canonical, ("aniworld" if provider.name == "AniWorld" else "sto")


def _remote_inventory(series_url, language):
    """Return the series model and its episodes available in one language."""
    provider = resolve_provider(series_url)
    series = provider.series_cls(url=series_url)
    inventory = {}
    for season in series.seasons:
        # Auto-Sync is episodic. AniWorld exposes associated films as a
        # pseudo-season, but their filenames have no SxxExx identity and would
        # otherwise be queued again on every run.
        if getattr(season, "are_movies", False):
            continue
        episodes = list(season.episodes)
        available = season.episode_languages
        if episodes and not available:
            # Usually a season that is announced but not streamable yet. Leave
            # it out for now: it is not in the baseline either, so its
            # episodes count as new once the site lists their languages.
            logger.warning(
                "AutoSync: no episode languages for season %s of %s, skipping it",
                season.season_number,
                series_url,
            )
            continue
        for episode in episodes:
            number = episode.episode_number
            if number is None or language not in available.get(number, ()):
                continue
            inventory[episode.url] = (season.season_number, number)
    return series, inventory


def _validated_provider(name):
    if name is not None and not isinstance(name, str):
        raise TypeError("provider must be a string.")
    chosen = (name or _default_provider()).strip()
    if chosen not in WORKING_PROVIDERS:
        raise ValueError(f"Unsupported hoster: {chosen}")
    return chosen


def add_subscription(
    *, series_url, language, provider=None, custom_path_id=None, subtitle_language="none"
):
    """Validate and add one tracked copy, recording its language-specific baseline."""
    canonical, site = canonical_series_url(series_url)
    if not isinstance(language, str):
        raise TypeError("language must be a string.")
    if language not in SITE_LANGUAGES[site]:
        raise ValueError(f"{language or 'The selected language'} is not supported here.")
    if not isinstance(subtitle_language, str):
        raise TypeError("subtitle_language must be a string.")
    subtitle_language = subtitle_language.strip().lower() or "none"
    if subtitle_language not in ("none", "deu"):
        raise ValueError("subtitle_language must be 'none' or 'deu'.")
    if site != "sto" and subtitle_language != "none":
        raise ValueError("Soft subtitles are currently supported only for SerienStream.")
    if custom_path_id is not None:
        if isinstance(custom_path_id, bool) or not isinstance(custom_path_id, int):
            raise TypeError("custom_path_id must be an integer or null.")
        if not db.get_custom_path(custom_path_id):
            raise ValueError("The selected custom download path no longer exists.")

    chosen_provider = _validated_provider(provider)
    _ensure_copy_is_unique(canonical, language, custom_path_id)
    series, inventory = _remote_inventory(canonical, language)
    title = (series.title or "").strip()
    if not title:
        raise RuntimeError("Could not read the series title from the source.")
    series_id = db.add_autosync_series(
        series_url=canonical,
        site=site,
        title=title,
        language=language,
        provider=chosen_provider,
        subtitle_language=subtitle_language,
        custom_path_id=custom_path_id,
        baseline_episodes=sorted(_episode_key(numbers) for numbers in inventory.values()),
    )
    return db.get_autosync_series_item(series_id)


def _subscription_base(subscription):
    path_id = subscription.get("custom_path_id")
    if path_id is not None and not db.get_custom_path(path_id):
        raise RuntimeError("The configured custom download path no longer exists.")
    base = paths.base_for(path_id)
    if paths.lang_separation_enabled():
        base = base / paths.lang_folder_for(subscription["language"])
    return base


def _matching_folders(base, title):
    if not base.is_dir():
        return []
    try:
        return [
            child
            for child in base.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and folder_matches_title(child.name, title)
        ]
    except OSError:
        return []


def episodes_in_folder(folder, language=None, subtitle_language="none"):
    """Episode numbers whose audio/video and requested soft subtitles are present."""
    from ..models.common.subtitles import normalize_subtitle_language
    from .library import VIDEO_EXTENSIONS
    from .media import EPISODE_RE

    subtitle_language = normalize_subtitle_language(subtitle_language)
    found = set()
    try:
        files = folder.rglob("*")
    except OSError:
        return found
    for file in files:
        try:
            if not file.is_file() or file.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
        except OSError:
            continue
        match = EPISODE_RE.search(file.name)
        if not match:
            continue
        if language:
            detected_languages = languages_from_probe(file)
            # An absent/failed language probe must not make Auto-Sync queue an
            # episode that is visibly already on disk. Only reject the file
            # when its metadata positively identifies a different language.
            if detected_languages and language not in detected_languages:
                continue
        if subtitle_language != "none":
            detected_subtitles = subtitle_languages_from_probe(file)
            if subtitle_language not in detected_subtitles:
                continue
        found.add((int(match.group(1)), int(match.group(2))))
    return found


def _downloaded(subscription):
    separated = paths.lang_separation_enabled()
    have = set()
    for folder in _matching_folders(_subscription_base(subscription), subscription["title"]):
        have |= episodes_in_folder(
            folder,
            None if separated else subscription["language"],
            subscription.get("subtitle_language") or "none",
        )
    return have


def _where(subscription):
    root = subscription.get("custom_path_name") or "Default"
    if paths.lang_separation_enabled():
        return f"{root} / {paths.lang_folder_for(subscription['language'])}"
    return root


def _baseline(subscription):
    try:
        value = json.loads(subscription.get("baseline_episodes") or "[]")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("The saved Auto-Sync baseline is invalid.") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError("The saved Auto-Sync baseline is invalid.")
    return set(value)


def _episode_key(numbers):
    """Stable baseline identity, independent of a SerienStream mirror hostname."""
    return f"{numbers[0]}:{numbers[1]}"


def _default_provider():
    from ..config import get_provider_fallback_order

    order = list(get_provider_fallback_order(WORKING_PROVIDERS))
    return order[0] if order else "VOE"


def _ensure_copy_is_unique(series_url, language, custom_path_id, current_id=None):
    """Reject duplicate or mutually overwriting tracked copies."""
    for existing in db.get_autosync_series():
        if current_id is not None and existing["id"] == current_id:
            continue
        if (
            existing["series_url"] != series_url
            or existing.get("custom_path_id") != custom_path_id
        ):
            continue
        if existing["language"] == language:
            raise DuplicateSubscription("This series copy is already in Auto-Sync.")
        if not paths.lang_separation_enabled() and existing["enabled"]:
            raise RuntimeError(
                "Two languages for the same series and destination require "
                "language-separated folders."
            )


def _handle(subscription):
    title = subscription["title"]
    series_url = subscription["series_url"]
    language = subscription["language"]
    report = {
        "id": subscription["id"],
        "title": title,
        "series_url": series_url,
        "site": subscription["site"],
        "language": language,
        "subtitle_language": subscription.get("subtitle_language") or "none",
        "where": _where(subscription),
    }

    _ensure_copy_is_unique(
        series_url,
        language,
        subscription.get("custom_path_id"),
        current_id=subscription["id"],
    )

    if db.is_copy_queued_or_running(
        series_url, language, subscription.get("custom_path_id")
    ):
        db.update_autosync_series(
            subscription["id"],
            last_checked_at=_now().isoformat(),
            last_error=None,
        )
        return {
            **report,
            "status": "skipped",
            "reason": "This copy is already in the queue.",
        }

    series, inventory = _remote_inventory(series_url, language)
    eligible = set(inventory)
    if autosync_new_only():
        baseline = _baseline(subscription)
        eligible = {
            url for url in eligible if _episode_key(inventory[url]) not in baseline
        }
    have = _downloaded(subscription)
    missing = [url for url in eligible if inventory[url] not in have]
    missing.sort(key=lambda url: inventory[url])

    checked_at = _now().isoformat()
    db.update_autosync_series(
        subscription["id"],
        title=series.title or title,
        last_checked_at=checked_at,
        last_error=None,
    )
    if not missing:
        return {**report, "status": "up-to-date"}
    queue_id = db.add_to_queue(
        title=series.title or title,
        series_url=series_url,
        episodes=missing,
        language=language,
        provider=subscription["provider"],
        subtitle_language=subscription.get("subtitle_language") or "none",
        custom_path_id=subscription.get("custom_path_id"),
        source="autosync",
    )
    return {
        **report,
        "status": "queued",
        "episodes": len(missing),
        "queue_id": queue_id,
    }


def run_cycle():
    """Check each enabled list entry independently and store a report."""
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("A sync is already running")

    started = _now()
    try:
        subscriptions = [row for row in db.get_autosync_series() if row["enabled"]]
        rows = []
        for subscription in subscriptions:
            try:
                rows.append(_handle(subscription))
            except Exception as exc:
                logger.error("AutoSync failed for %s: %s", subscription["title"], exc)
                message = str(exc)[:200]
                db.update_autosync_series(
                    subscription["id"],
                    last_checked_at=_now().isoformat(),
                    last_error=message,
                )
                rows.append(
                    {
                        "id": subscription["id"],
                        "title": subscription["title"],
                        "series_url": subscription["series_url"],
                        "site": subscription["site"],
                        "language": subscription["language"],
                        "where": _where(subscription),
                        "status": "error",
                        "reason": message,
                    }
                )

        report = {
            "started_at": started.isoformat(),
            "finished_at": _now().isoformat(),
            "checked": len(subscriptions),
            "queued": sum(1 for row in rows if row["status"] == "queued"),
            "results": rows,
        }
        db.set_autosync_state(last_run=started.isoformat(), last_report=_dump(report))
        logger.info(
            "AutoSync finished: %d checked, %d queued",
            report["checked"],
            report["queued"],
        )
        if report["queued"]:
            from . import worker

            worker.ensure_started()
        return report
    finally:
        _run_lock.release()


def _dump(report):
    return json.dumps(report)


def is_running():
    return _run_lock.locked()


def status():
    """What the AutoSync page shows."""
    state = db.get_autosync_state()
    last_run = _parse(state.get("last_run"))
    report = None
    if state.get("last_report"):
        try:
            report = json.loads(state["last_report"])
        except ValueError:
            report = None

    fixed = _fixed_times()
    interval_seconds = autosync_interval_seconds()
    upcoming = next_run_at()
    return {
        "enabled": autosync_enabled(),
        "new_only": autosync_new_only(),
        "running": _run_lock.locked(),
        "tracked": len(db.get_autosync_series()),
        "mode": autosync_mode(),
        "interval": schedule.format_interval(interval_seconds),
        "interval_seconds": interval_seconds,
        "interval_hours": round(interval_seconds / 3600, 4),
        "cron": fixed.expression if fixed else None,
        "schedule": autosync_schedule_description(),
        "last_run": last_run.isoformat() if last_run else None,
        "next_run": upcoming.isoformat() if upcoming else None,
        "last_report": report,
        "providers": list(WORKING_PROVIDERS),
        "languages": {key: list(value) for key, value in SITE_LANGUAGES.items()},
    }


def _fixed_times():
    """The parsed fixed times, or None for interval mode and a broken schedule."""
    try:
        return autosync_cron_schedule()
    except schedule.ScheduleError as exc:
        logger.error(
            "AutoSync: unusable schedule, falling back to the interval: %s", exc
        )
        return None


def _anchor():
    """Stands in for last run while Auto-Sync has never run."""
    global _anchored_at
    if _anchored_at is None:
        _anchored_at = _now()
    return _anchored_at


def _reset_anchor():
    global _anchored_at
    _anchored_at = _now()


def next_run_at():
    """When the next cycle is due, in UTC, or None if it never fires."""
    last_run = _parse(db.get_autosync_state().get("last_run"))
    fixed = _fixed_times()
    if last_run is not None and last_run - _now() > _CLOCK_SLACK:
        logger.warning(
            "AutoSync: the last run is in the future (%s), ignoring it", last_run
        )
        last_run = None
    if fixed is None:
        if last_run is None:
            return _anchor()
        return last_run + timedelta(seconds=autosync_interval_seconds())
    upcoming = fixed.next_run(_local(last_run or _anchor()))
    return _utc(upcoming) if upcoming else None


def _due():
    upcoming = next_run_at()
    return upcoming is not None and _now() >= upcoming


def _nap_seconds():
    if not autosync_enabled():
        return TICK_SECONDS
    try:
        upcoming = next_run_at()
    except Exception:
        logger.exception("AutoSync: could not work out the next run")
        return TICK_SECONDS
    if upcoming is None:
        return TICK_SECONDS
    seconds = (upcoming - _now()).total_seconds()
    return max(MIN_TICK_SECONDS, min(TICK_SECONDS, seconds))


def _loop():
    while True:
        try:
            if autosync_enabled():
                if _due():
                    run_cycle()
            else:
                _reset_anchor()
        except Exception:
            logger.exception("AutoSync worker error")
        time.sleep(_nap_seconds())


def ensure_started():
    """Start the scheduling thread once per process."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    _anchor()
    threading.Thread(target=_loop, name="h0melab-autosync", daemon=True).start()
