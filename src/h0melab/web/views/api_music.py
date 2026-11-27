"""Music API: Soulseek search and downloads through slskd."""

from flask import jsonify, request, send_file

from .. import soulseek


def register(bp):
    bp.add_url_rule("/music/status", view_func=music_status)
    bp.add_url_rule("/music/discover", view_func=music_discover)
    bp.add_url_rule("/music/search", view_func=music_search, methods=["POST"])
    bp.add_url_rule("/music/search/<search_id>", view_func=music_search_state)
    bp.add_url_rule(
        "/music/search/<search_id>", view_func=music_search_delete,
        methods=["DELETE"], endpoint="music_search_delete",
    )
    bp.add_url_rule("/music/download", view_func=music_download, methods=["POST"])
    bp.add_url_rule("/music/cover", view_func=music_cover_fetch, methods=["POST"])
    bp.add_url_rule("/music/cover/<key>", view_func=music_cover)
    bp.add_url_rule("/music/transfers", view_func=music_transfers)
    bp.add_url_rule(
        "/music/transfers/cancel", view_func=music_cancel, methods=["POST"]
    )
    bp.add_url_rule(
        "/music/transfers/clear", view_func=music_clear, methods=["POST"]
    )
    bp.add_url_rule("/settings/soulseek/test", view_func=soulseek_test, methods=["POST"])


def _error(exc):
    return jsonify({"error": str(exc)}), exc.status


def music_status():
    if not soulseek.configured():
        return jsonify({"configured": False})
    try:
        return jsonify({"configured": True, **soulseek.status()})
    except soulseek.SoulseekError as exc:
        return jsonify({"configured": True, "reachable": False, "error": str(exc)})


def music_discover():
    from .. import music_discover as discover
    from .api_catalog import _proxy_cover

    row = (request.args.get("row") or "").strip()
    try:
        found = discover.items(row)
    except discover.DiscoverError as exc:
        status = 400 if str(exc) == "Unknown row" else 502
        return jsonify({"error": str(exc)}), status
    # Same signed proxy as the book covers: the browser never calls Deezer
    return jsonify({"items": [_proxy_cover(dict(item)) for item in found]})


def music_search():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"id": soulseek.start_search(body.get("q"))})
    except soulseek.SoulseekError as exc:
        return _error(exc)


def music_search_state(search_id):
    try:
        return jsonify(soulseek.search_state(search_id))
    except soulseek.SoulseekError as exc:
        return _error(exc)


def music_search_delete(search_id):
    try:
        soulseek.delete_search(search_id)
        return jsonify({"ok": True})
    except soulseek.SoulseekError as exc:
        return _error(exc)


def music_download():
    body = request.get_json(silent=True) or {}
    try:
        count = soulseek.download(body.get("username"), body.get("files"))
        return jsonify({"queued": count})
    except soulseek.SoulseekError as exc:
        return _error(exc)


def music_cover_fetch():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(
            soulseek.fetch_cover(body.get("username"), body.get("filename"), body.get("size"))
        )
    except soulseek.SoulseekError as exc:
        return _error(exc)


def music_cover(key):
    found = soulseek.cached_cover(key)
    if found is None:
        return "", 404
    path, mimetype = found
    response = send_file(path, mimetype=mimetype, max_age=86400)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def music_transfers():
    try:
        return jsonify({"transfers": soulseek.transfers()})
    except soulseek.SoulseekError as exc:
        return _error(exc)


def music_cancel():
    body = request.get_json(silent=True) or {}
    try:
        soulseek.cancel(body.get("username"), body.get("ids"), bool(body.get("remove")))
        return jsonify({"ok": True})
    except soulseek.SoulseekError as exc:
        return _error(exc)


def music_clear():
    try:
        soulseek.clear_finished()
        return jsonify({"ok": True})
    except soulseek.SoulseekError as exc:
        return _error(exc)


def soulseek_test():
    """Check the entered (not yet saved) slskd URL and key."""
    from ..settings_store import SettingsError

    body = request.get_json(silent=True)
    try:
        return jsonify(soulseek.status(body if isinstance(body, dict) else None))
    except SettingsError as exc:
        return jsonify({"error": str(exc)}), 400
    except soulseek.SoulseekError as exc:
        return _error(exc)
