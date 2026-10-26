"""Download queue and captcha endpoints."""

import hashlib
import json

from flask import Response, current_app, jsonify, request

from ...config import is_sto_host
from ...logger import get_logger
from .. import db, media_targets, video_library, worker
from ..media import mangafire_format
from ..settings_store import english_sub_disabled

logger = get_logger(__name__)


def register(bp):
    bp.add_url_rule("/download", view_func=start_download, methods=["POST"])
    bp.add_url_rule("/queue", view_func=list_queue)
    bp.add_url_rule("/queue/counts", view_func=queue_counts)
    bp.add_url_rule("/queue/completed", view_func=clear_completed, methods=["DELETE"])
    bp.add_url_rule("/queue/<int:queue_id>", view_func=remove_item, methods=["DELETE"])
    bp.add_url_rule(
        "/queue/<int:queue_id>/cancel", view_func=cancel_item, methods=["POST"]
    )
    bp.add_url_rule(
        "/queue/<int:queue_id>/force-cancel",
        view_func=force_cancel_item,
        methods=["POST"],
    )
    bp.add_url_rule("/queue/<int:queue_id>/move", view_func=move_item, methods=["POST"])
    bp.add_url_rule(
        "/queue/<int:queue_id>/retry", view_func=retry_item, methods=["POST"]
    )
    bp.add_url_rule(
        "/queue/<int:queue_id>/pause", view_func=pause_item, methods=["POST"]
    )
    bp.add_url_rule(
        "/queue/<int:queue_id>/resume", view_func=resume_item, methods=["POST"]
    )
    bp.add_url_rule("/captcha/<int:queue_id>/screenshot", view_func=captcha_screenshot)
    bp.add_url_rule("/captcha/<int:queue_id>/status", view_func=captcha_status)
    bp.add_url_rule(
        "/captcha/<int:queue_id>/click", view_func=captcha_click, methods=["POST"]
    )


def _current_username():
    if not current_app.config.get("AUTH_ENABLED", False):
        return None
    from ..auth import current_username

    return current_username()


def start_download():
    data = request.get_json(silent=True) or {}
    request_key = str(data.get("request_key") or "").strip()
    if len(request_key) > 128:
        return jsonify({"error": "request_key is too long"}), 400
    episodes = data.get("episodes") or []
    if not episodes:
        return jsonify({"error": "episodes list is required"}), 400
    if not isinstance(episodes, list) or len(episodes) > video_library.MAX_ITEMS:
        return jsonify({"error": "Select at most 20000 media entries"}), 400
    if any(
        isinstance(entry, dict) and any(key.startswith("_media_") for key in entry)
        for entry in episodes
    ):
        return jsonify({"error": "Internal media-routing fields are not accepted"}), 400
    if any(
        isinstance(entry, dict) and "connector_protocol" in entry for entry in episodes
    ):
        return jsonify(
            {"error": "Internal connector markers are not accepted from clients"}
        ), 400
    if data.get("connector_protocol") is not None:
        from .. import apikeys
        from ..auth import get_current_user

        key = apikeys.current()
        user = get_current_user()
        if (key is not None and key["scope"] != "admin") or (
            key is None
            and current_app.config.get("AUTH_ENABLED", False)
            and (not user or user["role"] != "admin")
        ):
            return jsonify(
                {"error": "Connector file receipts require full access"}
            ), 403
        from .. import connector_files

        if (
            data["connector_protocol"] != 1
            or not request_key
            or not isinstance(episodes, list)
            or len(episodes) > video_library.MAX_ITEMS
        ):
            return jsonify({"error": "Invalid connector download contract"}), 400
        try:
            for entry in episodes:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("url"), str)
                    or entry.get("media_type") not in ("movie", "series")
                ):
                    raise ValueError("Invalid connector entry")
                if entry.get("language") == "English Sub" and english_sub_disabled():
                    return jsonify({"error": "English Sub downloads are disabled"}), 403
                if entry.get("language") not in (
                    "German Dub",
                    "English Dub",
                    "German Sub",
                    "English Sub",
                    "Japanese",
                ):
                    raise ValueError("Unknown audio/subtitle variant")
                if (
                    not isinstance(entry.get("provider"), str)
                    or not entry["provider"]
                    or len(entry["provider"]) > 100
                ):
                    raise ValueError("Invalid provider")
                root = connector_files.root_for(entry.get("custom_path_id"))
                if str(root) != entry.get("target_root") or not root.is_dir():
                    raise ValueError("Download root changed or disappeared")
                entry["connector_protocol"] = 1
        except (ValueError, TypeError):
            return jsonify(
                {
                    "error": "Connector target or variant is invalid; refresh the selection"
                }
            ), 409

    language = data.get("language", "German Dub")
    if language == "English Sub" and english_sub_disabled():
        return jsonify({"error": "English Sub downloads are disabled"}), 403

    provider = data.get("provider", "VOE")
    subtitle_language = data.get("subtitle_language", "none")
    if not isinstance(subtitle_language, str):
        return jsonify({"error": "subtitle_language must be a string"}), 400
    subtitle_language = subtitle_language.strip().lower() or "none"
    if subtitle_language not in ("none", "deu"):
        return jsonify({"error": "subtitle_language must be 'none' or 'deu'"}), 400
    sample_url = (
        episodes[0].get("url", "") if isinstance(episodes[0], dict) else episodes[0]
    )
    is_serienstream = is_sto_host(data.get("series_url", "")) or is_sto_host(
        str(sample_url)
    )
    if subtitle_language != "none" and not is_serienstream:
        return jsonify(
            {"error": "Soft subtitles are currently supported only for SerienStream"}
        ), 400
    if provider == "MangaFire":
        episodes = _tag_mangafire(episodes, data.get("mangafire_format"))

    routed = media_targets.read()["enabled"] and provider != "MangaFire"
    if routed:
        try:
            prepared = []
            for raw in episodes:
                entry = dict(raw) if isinstance(raw, dict) else {"url": raw}
                identity = video_library.inspect(
                    entry["url"], entry.get("language", language)
                )
                if (
                    entry.get("connector_protocol") == 1
                    and entry["media_type"] != identity["media_type"]
                ):
                    raise ValueError(
                        "The source changed the movie/show type; reopen this title"
                    )
                chosen = entry.get("custom_path_id", data.get("custom_path_id"))
                target, root, revision = media_targets.resolve(
                    identity["media_type"], chosen, entry.get("target_root")
                )
                entry.update(
                    _media_identity=identity,
                    _media_revision=revision,
                    media_type=identity["media_type"],
                    custom_path_id=target,
                    target_root=str(root),
                )
                prepared.append(entry)
            episodes = prepared
        except (ValueError, OSError, KeyError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 409

    native_key = routed and not request_key
    if native_key:
        from .. import apikeys

        key = apikeys.current()
        identity = _current_username() or ("key:" + str(key["id"]) if key else "local")
        signature = json.dumps(
            [identity, episodes, language, provider, subtitle_language], sort_keys=True
        )
        request_key = "native:" + hashlib.sha256(signature.encode()).hexdigest()
    queue_id = db.add_to_queue(
        title=data.get("title", "Unknown"),
        series_url=data.get("series_url", ""),
        episodes=episodes,
        language=language,
        provider=provider,
        subtitle_language=subtitle_language,
        username=_current_username(),
        custom_path_id=data.get("custom_path_id"),
        request_key=request_key or None,
        active_request_key=native_key,
        payload_version=3
        if routed
        else 2
        if data.get("connector_protocol") == 1
        else 1,
    )
    worker.ensure_started()
    return jsonify(
        {
            "queue_id": queue_id,
            "accepted_episode_count": db.get_queue_item(queue_id)["total_episodes"],
        }
    )


def _tag_mangafire(episodes, requested_format):
    """MangaFire needs the output format stored per queued chapter."""
    output = requested_format or mangafire_format()
    tagged = []
    for entry in episodes:
        if isinstance(entry, dict):
            tagged.append({**entry, "mangafire_format": output})
        else:
            tagged.append({"url": entry, "mangafire_format": output})
    return tagged


# What ?status= accepts: a real status, or one of the two groups.
_STATUS_FILTERS = frozenset(
    (
        "queued",
        "running",
        "paused",
        "completed",
        "failed",
        "cancelled",
        "active",
        "finished",
    )
)
_SORTS = frozenset(("smart", "newest", "oldest", "title"))

# Passing any of these switches the response to a page of the queue.
_PAGE_ARGS = ("limit", "offset", "status", "q", "sort")


def list_queue():
    """The queue, whole or a page of it.

    Without any query parameters this returns every row, which is what the
    documented API has always done and what scripts expect. Pass any of the
    paging parameters and you get one page plus the totals the pager needs.
    Paged rows leave out `episodes`, the largest column by far and one the list
    never displays; fetch the queue unpaged if you need it.
    """
    from ...models.common.common import get_ffmpeg_progress

    args = request.args
    progress = get_ffmpeg_progress()
    if not any(arg in args for arg in _PAGE_ARGS):
        return jsonify(
            {
                "items": db.get_queue(),
                "ffmpeg_progress": progress,
                "episode_progress": progress,
            }
        )

    status = args.get("status") or None
    if status and status not in _STATUS_FILTERS:
        return jsonify({"error": f"unknown status filter: {status}"}), 400

    sort = args.get("sort") or "smart"
    if sort not in _SORTS:
        return jsonify({"error": f"unknown sort: {sort}"}), 400

    try:
        limit = int(args.get("limit", 25))
        offset = int(args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "limit and offset must be whole numbers"}), 400
    if limit < 1 or offset < 0:
        return jsonify(
            {"error": "limit must be positive and offset cannot be negative"}
        ), 400

    items, total = db.get_queue_page(
        status=status,
        search=(args.get("q") or "").strip() or None,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return jsonify(
        {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "counts": db.queue_counts(),
            "ffmpeg_progress": progress,
            "episode_progress": progress,
        }
    )


def queue_counts():
    """Just the numbers, so the nav badge does not pull the whole queue."""
    return jsonify({"counts": db.queue_counts()})


def _result(ok, error, status=400):
    return (jsonify({"ok": True}), 200) if ok else (jsonify({"error": error}), status)


def remove_item(queue_id):
    return _result(*db.remove_from_queue(queue_id))


def cancel_item(queue_id):
    return _result(*db.cancel_queue_item(queue_id))


def force_cancel_item(queue_id):
    return _result(*db.cancel_queue_item(queue_id, force=True))


def move_item(queue_id):
    data = request.get_json(silent=True) or {}
    return _result(*db.move_queue_item(queue_id, (data.get("direction") or "").strip()))


def retry_item(queue_id):
    """Re-queue a failed or cancelled item, e.g. after solving the kinox captcha."""
    if not db.requeue_item(queue_id):
        return jsonify({"error": "Item not found or not retryable"}), 400
    worker.ensure_started()
    return jsonify({"ok": True})


def pause_item(queue_id):
    return _result(*db.pause_queue_item(queue_id))


def resume_item(queue_id):
    ok, error = db.resume_queue_item(queue_id)
    if not ok:
        return jsonify({"error": error}), 400
    worker.ensure_started()
    return jsonify({"ok": True})


def clear_completed():
    db.clear_completed()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Captcha (the playwright browser is streamed into the queue modal)
# ---------------------------------------------------------------------------
def _captcha_session(queue_id):
    from ...playwright.captcha import _active_sessions, _active_sessions_lock

    with _active_sessions_lock:
        return _active_sessions.get(queue_id)


def captcha_screenshot(queue_id):
    session = _captcha_session(queue_id)
    data = session.get_screenshot() if session else None
    if not data:
        return "", 404
    return Response(
        data,
        mimetype="image/jpeg",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def captcha_status(queue_id):
    session = _captcha_session(queue_id)
    if not session:
        return jsonify({"active": False})
    return jsonify({"active": True, "done": session.done})


def captcha_click(queue_id):
    data = request.get_json(silent=True) or {}
    x, y = data.get("x"), data.get("y")
    if x is None or y is None:
        return jsonify({"error": "x and y are required"}), 400

    session = _captcha_session(queue_id)
    if not session:
        return jsonify({"error": "No active captcha session"}), 404
    session.enqueue_click(int(x), int(y))
    return jsonify({"ok": True})
