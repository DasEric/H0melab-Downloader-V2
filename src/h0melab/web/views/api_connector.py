"""Full-access connector file receipts and mount verification."""

import secrets
import uuid

from flask import jsonify, request

from .. import connector_files, media_targets


def register(bp):
    bp.add_url_rule("/media-targets", view_func=media_targets_get, methods=["GET"])
    bp.add_url_rule("/media-targets", view_func=media_targets_save, methods=["PUT"])
    bp.add_url_rule("/connector/capabilities", view_func=connector_capabilities)
    bp.add_url_rule(
        "/connector/artifacts/query", view_func=connector_artifacts, methods=["POST"]
    )
    bp.add_url_rule(
        "/connector/artifacts/delete", view_func=connector_delete, methods=["POST"]
    )
    bp.add_url_rule(
        "/connector/path-probe", view_func=connector_path_probe, methods=["POST"]
    )


def connector_capabilities():
    return jsonify(connector_files.capabilities())


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


def connector_artifacts():
    try:
        return jsonify(
            {
                "items": connector_files.query(
                    (request.get_json(silent=True) or {}).get("request_keys")
                )
            }
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


def connector_delete():
    try:
        return jsonify(
            {
                "items": connector_files.delete(
                    (request.get_json(silent=True) or {}).get("ids")
                )
            }
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


def connector_path_probe():
    data = request.get_json(silent=True) or {}
    try:
        path_id = data.get("path_id")
        if not isinstance(path_id, int) or isinstance(path_id, bool) or path_id <= 0:
            raise ValueError("Select a custom download path")
        root = connector_files.root_for(path_id)
        if not root.is_dir():
            raise ValueError("Download root does not exist")
        identity = data.get("probe_id") or uuid.uuid4().hex
        if (
            not isinstance(identity, str)
            or len(identity) != 32
            or any(c not in "0123456789abcdef" for c in identity)
        ):
            raise ValueError("Invalid probe identity")
        path = connector_files.checked_path(root, root / f".connector-probe-{identity}")
        if data.get("cleanup"):
            path.unlink(missing_ok=True)
            return jsonify({"ok": True})
        nonce = secrets.token_hex(32)
        with path.open("x", encoding="utf-8") as output:
            output.write(nonce)
        return jsonify(
            {"probe_id": identity, "relative_path": path.name, "nonce": nonce}
        )
    except (OSError, ValueError):
        return jsonify({"error": "Download path verification failed"}), 409
