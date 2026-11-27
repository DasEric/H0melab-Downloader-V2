"""Background download worker.

One global thread processes the queue a single item at a time, so downloads
never fight over ffmpeg or the captcha browser.
"""

import json
import threading
import time

from ..logger import get_logger
from ..providers import resolve_provider
from . import db, media_targets, paths, settings_store, video_library
from .media import mangafire_format

logger = get_logger(__name__)

_started = False
_start_lock = threading.Lock()

# How long to wait before looking at the queue again when it is empty.
IDLE_SECONDS = 3


def ensure_started():
    """Start the worker thread once per process."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        try:
            threading.Thread(target=_run, name="h0melab-queue", daemon=True).start()
        except Exception:
            _started = False
            raise


def _claim_next():
    """Claim atomically, including the existing single-worker busy check."""
    return db.claim_next_queued()


def _run():
    # Standby processes never reset another worker's rows. Kernel ownership is
    # released even after an unclean process death, so a standby can recover.
    while True:
        try:
            with db.worker_ownership() as acquired:
                if acquired:
                    db._reset_stale_running_owned()
                    _run_owned()
        except Exception:
            logger.error("Queue worker ownership/recovery failed; retrying")
        time.sleep(IDLE_SECONDS)


def _run_owned():
    while True:
        item = None
        try:
            item = _claim_next()
            if not item:
                time.sleep(IDLE_SECONDS)
                continue
            _process(item)
        except Exception:
            if item and item.get("source") == "seerr":
                logger.error("Seerr worker failed for queue item %s", item["id"])
            else:
                logger.exception("Queue worker error")
            if item:
                # Propagate status-write failure so ownership is released and recovery can retry.
                if item.get("source") == "seerr":
                    from . import seerr_files
                    from .seerr_processing import stopped
                    current = db.get_queue_item(item["id"])
                    # A notification failure after commit must not undo completion.
                    if current and current["status"] == "running" and not stopped(item):
                        seerr_files.finish(item, "failed")
                else:
                    db.set_queue_status(item["id"], "failed")
            time.sleep(IDLE_SECONDS)


def _episode_request(entry):
    """Normalise a queued entry into (url, extra episode kwargs)."""
    if not isinstance(entry, dict):
        return str(entry), {}

    url = (entry.get("url") or "").strip()
    extra = {}
    if entry.get("selected_pages") is not None:
        extra["selected_pages"] = entry["selected_pages"]
    if entry.get("series_url"):
        extra["_series_url"] = entry["series_url"]
    extra["_format"] = entry.get("mangafire_format", mangafire_format())
    return url, extra


def _connector_entry(item, entry):
    """True only for entries the server itself marked during connector admission."""
    return bool(
        isinstance(entry, dict)
        and entry.get("connector_protocol") == 1
        and item.get("payload_version") in (2, 3)
        and item.get("request_key")
    )


def _record_connector_file(item, index, output, root, existed, options):
    """Store the exact-file receipt and reject a file with the wrong audio."""
    from . import connector_files

    connector_files.record(item, index, output, root, existed, options)
    if (
        not existed
        and connector_files.audio_matches(
            connector_files.media_streams(output), options["language"]
        )
        is False
    ):
        raise ValueError("Downloaded audio does not match the requested language")


def _build_episode(url, extra, item, selected_path):
    url = video_library.variant_url(url, item["language"], item["provider"])
    provider = resolve_provider(url)
    kwargs = {
        "url": url,
        "selected_language": item["language"],
        "selected_provider": item["provider"],
    }

    series = None
    if provider.name == "MangaFire":
        series_url = extra.get("_series_url") or url.rsplit("/chapter/", 1)[0]
        try:
            series = provider.series_cls(url=series_url)
        except Exception:
            series = None
        kwargs["format"] = extra["_format"]
    if provider.name in ("SerienStream", "ZDF", "DMAX"):
        kwargs["selected_subtitle_language"] = item.get("subtitle_language") or "none"

    # MegaKino episodes build their own series context internally
    if series is not None and provider.name != "MegaKino":
        kwargs["series"] = series
    if "selected_pages" in extra:
        kwargs["selected_pages"] = extra["selected_pages"]
    if selected_path:
        kwargs["selected_path"] = selected_path

    return provider, provider.episode_cls(**kwargs)


def _process(item):
    from ..playwright import captcha

    queue_id = item["id"]
    if item.get("source") == "legacy_review":
        raise ValueError("Retired request: review and submit this media through Seerr")
    if item.get("source") == "seerr":
        from . import seerr
        if not seerr.prepare(item):
            return
    entries = json.loads(item["episodes"])
    if item.get("source") == "catalog":
        return _process_catalog(item, entries)
    selected_path = paths.target_path(item["language"], item.get("custom_path_id"))
    errors = []
    all_existing = True

    start_index = max(0, min(int(item.get("current_episode") or 0), len(entries)))
    for index, entry in _queued_entries(entries, start_index):
        url, extra = _episode_request(entry)
        provider = None
        try:
            if item.get("source") == "seerr":
                from .seerr_processing import stopped
                if stopped(item):
                    return
            if db.queue_control_flags(queue_id)[2]:
                db.finish_pause(queue_id)
                return
            db.update_queue_progress(queue_id, index, url)
            if item.get("source") == "seerr":
                from . import seerr_files
                if seerr_files.existing(entry):
                    db.update_queue_progress(queue_id, index + 1, "")
                    continue
                if entry.get("_receipt_only"):
                    from .seerr_processing import review
                    review(item, "Previously verified episode disappeared; retry to resolve its source")
                    return
            entry_options = dict(item)
            entry_path = selected_path
            connector_entry = _connector_entry(item, entry)
            if connector_entry:
                # Each connector entry carries its own verified variant and target.
                entry_options.update(
                    {
                        key: entry[key]
                        for key in (
                            "language",
                            "provider",
                            "media_type",
                            "custom_path_id",
                        )
                        if key in entry
                    }
                )
                if not media_targets.read()["enabled"]:
                    raise ValueError(
                        "Movie/show destinations were removed; configure them and retry"
                    )
            if item.get("source") != "seerr":
                provider, episode = _build_episode(url, extra, entry_options, entry_path)
            if item.get("source") == "seerr":
                pass  # Seerr applies its canonical layout and fresh targets below.
            elif media_targets.read()["enabled"] and provider.name != "MangaFire":
                stored = (
                    entry.get("_media_identity")
                    if isinstance(entry, dict) and item.get("payload_version") == 3
                    else None
                )
                identity = (
                    stored
                    or video_library.observed(url)
                    or video_library.identity_for_episode(episode, url)
                )
                if identity is None:
                    raise ValueError(
                        "Verify this old download's movie/show identity before retrying"
                    )
                fresh = video_library.identity_for_episode(episode, url)
                # Legacy movie wrappers carried dummy season/episode numbers.
                # Those are not part of a film's identity; its type still is.
                identity_fields = ("media_type",)
                if identity["media_type"] == "series":
                    identity_fields += ("season", "episode")
                if fresh and any(
                    fresh.get(key) != identity.get(key) for key in identity_fields
                ):
                    raise ValueError(
                        "Source identity changed since this title was queued"
                    )
                if provider.name == "Moflix" and identity["media_type"] == "movie":
                    # A legacy retry must reserve the same output as a new job,
                    # without changing its queued title/year or destination.
                    identity = {
                        key: value
                        for key, value in identity.items()
                        if key not in ("season", "episode", "end")
                    }
                selected = (
                    entry.get("custom_path_id")
                    if isinstance(entry, dict)
                    else item.get("custom_path_id")
                )
                _target, video_root, _ = media_targets.resolve(
                    identity["media_type"],
                    selected,
                    entry.get("target_root") if isinstance(entry, dict) else None,
                    entry.get("_media_revision") if isinstance(entry, dict) else None,
                )
                episode.selected_path = str(video_root)
                episode._video_identity = identity
                episode._video_root = video_root
                episode._media_variant = entry_options["language"]
                episode._media_root = video_root
                entry_options["media_type"] = identity["media_type"]
            elif (group := video_library.by_url(url)) is not None:
                episode._video_identity = {
                    **group,
                    "media_type": "series",
                    "end": group["end_episode"],
                }
                episode._video_root = entry_path or paths.base_for(
                    item.get("custom_path_id")
                )
            from ..models.common.common import (
                clear_episode_download_context,
                set_episode_download_context,
            )

            set_episode_download_context(
                queue_id, index, settings_store.hls_concurrency()
            )
            # Tells the captcha module to stream its browser into this queue item
            captcha._local.queue_id = queue_id
            try:
                refresh_kind = None
                if item.get("source") == "seerr":
                    from .seerr_processing import stopped
                    if stopped(item):
                        return
                    from . import seerr_files
                    from .seerr_fallback import download_entry
                    existed = download_entry(item, entry, entry_path, index)
                    if existed is None:
                        return
                    all_existing = existed and all_existing
                    if not existed:
                        refresh_kind = entry["_seerr_identity"]["media_type"]
                elif getattr(episode, "_video_identity", None):
                    from . import media_files

                    output = media_files.checked_path(
                        episode._video_root, episode._episode_path
                    )
                    video_library.reserve_output(output, episode._video_identity)
                    with media_files.file_operation(output):
                        existed = output.exists()
                        # Never remux or overwrite another request's existing file.
                        if not existed:
                            output.parent.mkdir(parents=True, exist_ok=True)
                            episode.download()
                            if output.is_file() and output.stat().st_size > 0:
                                refresh_kind = episode._video_identity["media_type"]
                        if connector_entry:
                            _record_connector_file(
                                item,
                                index,
                                output,
                                episode._video_root,
                                existed,
                                entry_options,
                            )
                else:
                    episode.download()
                if refresh_kind:
                    from . import jellyfin, plex
                    jellyfin.refresh(refresh_kind)
                    plex.refresh(refresh_kind)
            finally:
                captcha._local.queue_id = None
                clear_episode_download_context()
        except Exception as exc:
            captcha._local.queue_id = None
            from ..models.common.common import DownloadPaused

            if item.get("source") == "seerr":
                from .seerr_matching import IdentityReviewRequired
                if isinstance(exc, IdentityReviewRequired):
                    from .seerr_processing import review
                    db.update_queue_progress(queue_id, index, "")
                    review(item, str(exc))
                    return

            if isinstance(exc, DownloadPaused):
                logger.info("Download paused for queue item %s", queue_id)
                db.update_queue_progress(queue_id, index, url)
                db.finish_pause(queue_id)
                return
            # A force cancel kills the download on purpose. Whatever it raised
            # on the way down is the cancel, not a failure worth showing.
            if db.cancel_flags(queue_id)[1]:
                logger.info("Download of %s stopped by force cancel", url)
            else:
                if item.get("source") == "seerr":
                    # Provider exceptions can contain signed stream URLs or credentials.
                    label = (
                        f"S{int(entry.get('season') or 0):02d}E{int(entry.get('episode') or 0):02d}"
                        if isinstance(entry, dict)
                        else "entry"
                    )
                    root = exc.__cause__ or exc
                    logger.error(
                        "Seerr download failed for queue item %s, %s (%s)",
                        queue_id,
                        label,
                        type(root).__name__,
                    )
                    failure = {
                        "error": f"{label}: Download or source verification failed on every source; retry later"
                    }
                else:
                    logger.error("Download failed for %s: %s", url, exc)
                    failure = {"url": url, "error": str(exc)}
                errors.append(failure)
                db.update_queue_errors(queue_id, errors)

        cancelled, forced, paused = db.queue_control_flags(queue_id)
        if paused:
            db.update_queue_progress(queue_id, index + 1, "")
            db.finish_pause(queue_id)
            return
        if cancelled:
            logger.info(
                "Download %s for queue item %s",
                "force cancelled" if forced else "cancelled",
                queue_id,
            )
            # a forced stop killed this episode part way, it does not count
            done = index if forced else index + 1
            db.update_queue_progress(queue_id, done, "")
            # asking to stop after the last episode still leaves everything on disk
            everything_done = not forced and done >= len(entries) and not errors
            if item.get("source") != "seerr":
                db.set_queue_status(
                    queue_id, "completed" if everything_done else "cancelled"
                )
            if item.get("source") == "upcoming":
                db.finish_upcoming_queue(
                    queue_id, "completed" if everything_done else "cancelled"
                )
            if item.get("source") == "seerr":
                from . import seerr_files
                seerr_files.finish(item, "completed" if everything_done else "cancelled", all_existing)
            if everything_done and item.get("source") == "discord":
                _notify_discord(item)
            return

    db.update_queue_progress(queue_id, len(entries), "")
    status = "failed" if errors and (item.get("source") == "seerr" or len(errors) == len(entries)) else "completed"
    if item.get("source") != "seerr":
        db.set_queue_status(queue_id, status)
    if item.get("source") == "seerr":
        from . import seerr_files
        seerr_files.finish(item, status, all_existing)

    if item.get("source") == "upcoming":
        db.finish_upcoming_queue(queue_id, status)

    if status == "completed" and item.get("source") == "discord":
        _notify_discord(item)


def _queued_entries(entries, start):
    """One logical request, resumable bounded work sections of 500 entries."""
    for offset in range(start, len(entries), 500):
        yield from enumerate(entries[offset : offset + 500], start=offset)


def _process_catalog(item, entries):
    """Process book/audio assets without passing them to video providers."""
    from ..catalog.download import download_entry

    queue_id = item["id"]
    errors = []
    start_index = max(0, min(int(item.get("current_episode") or 0), len(entries)))
    for index, entry in enumerate(entries[start_index:], start=start_index):
        label = entry.get("asset", {}).get("label") or entry.get("asset", {}).get(
            "url", ""
        )
        try:
            if db.queue_control_flags(queue_id)[2]:
                db.finish_pause(queue_id)
                return
            db.update_queue_progress(queue_id, index, label)
            download_entry(queue_id, entry)
        except Exception as exc:
            from ..models.common.common import DownloadPaused

            if isinstance(exc, DownloadPaused):
                db.update_queue_progress(queue_id, index, label)
                db.finish_pause(queue_id)
                return
            if not db.cancel_flags(queue_id)[1]:
                logger.error("Catalog download failed for %s: %s", label, exc)
                errors.append({"url": label, "error": str(exc)})
                db.update_queue_errors(queue_id, errors)
        cancelled, forced, paused = db.queue_control_flags(queue_id)
        if paused:
            db.update_queue_progress(queue_id, index + 1, "")
            db.finish_pause(queue_id)
            return
        if cancelled:
            done = index if forced else index + 1
            db.update_queue_progress(queue_id, done, "")
            db.set_queue_status(queue_id, "cancelled")
            return
    db.update_queue_progress(queue_id, len(entries), "")
    db.set_queue_status(
        queue_id, "failed" if errors and len(errors) == len(entries) else "completed"
    )


def _notify_discord(item):
    try:
        from .discord_bot import notify_completed

        media_type = "movie" if int(item.get("total_episodes") or 1) <= 1 else "series"
        notify_completed(
            item.get("title") or "Unknown",
            media_type,
            item.get("language") or "",
            item.get("discord_user_id"),
        )
    except Exception as exc:
        logger.info("Discord completion notice skipped: %s", exc)
