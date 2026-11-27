"""Music API: Soulseek search, downloads and discovery (own aioslsk service)."""

from flask import jsonify, request, send_file

from .. import soulseek


def register(bp):
    bp.add_url_rule("/music/status", view_func=music_status)
    bp.add_url_rule("/music/discover", view_func=music_discover)
    bp.add_url_rule("/music/like", view_func=music_like, methods=["POST"])
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
    bp.add_url_rule("/settings/soulseek/restart", view_func=soulseek_restart, methods=["POST"])
    bp.add_url_rule("/settings/soulseek/update", view_func=soulseek_update, methods=["POST"])


def _error(exc):
    return jsonify({"error": str(exc)}), exc.status


def music_status():
    from .. import soulseek_runtime

    if not soulseek.configured():
        return jsonify({"configured": False})
    soulseek_runtime.ensure_started()
    runtime = soulseek_runtime.status()
    base = {"configured": True, "phase": runtime["phase"], "message": runtime["message"]}
    if runtime["phase"] != "running":
        return jsonify({**base, "reachable": False})
    try:
        return jsonify({**base, **soulseek.status()})
    except soulseek.SoulseekError as exc:
        return jsonify({**base, "reachable": False, "error": str(exc)})


def music_discover():
    row = (request.args.get("row") or "").strip()
    try:
        return jsonify({"items": soulseek.discover(row)})
    except soulseek.SoulseekError as exc:
        return _error(exc)


def music_like():
    body = request.get_json(silent=True) or {}
    try:
        return jsonify({"item": soulseek.like(body.get("item"))})
    except soulseek.SoulseekError as exc:
        return _error(exc)


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


def soulseek_restart():
    from .. import soulseek_runtime

    soulseek_runtime.restart()
    return jsonify(soulseek_runtime.status())


def soulseek_update():
    from .. import soulseek_runtime

    soulseek_runtime.update_now()
    return jsonify(soulseek_runtime.status())
