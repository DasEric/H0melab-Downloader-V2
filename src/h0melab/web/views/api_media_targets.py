"""Existing movie/show destination configuration endpoints."""

from flask import jsonify, request

from .. import media_targets


def register(bp):
    bp.add_url_rule("/media-targets", view_func=media_targets_get, methods=["GET"])
    bp.add_url_rule("/media-targets", view_func=media_targets_save, methods=["PUT"])


def media_targets_get():
    return jsonify(media_targets.read())


def media_targets_save():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(
            media_targets.save(
                data.get("movie"), data.get("series"), data.get("revision")
            )
        )
    except (ValueError, OSError) as exc:
        return jsonify({"error": str(exc)}), 409

