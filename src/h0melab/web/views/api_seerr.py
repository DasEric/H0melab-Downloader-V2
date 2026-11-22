"""Authenticated Seerr ingress and administrator request inspection."""

from flask import jsonify, request
from werkzeug.exceptions import BadRequest

from .. import seerr

MAX_PAYLOAD_BYTES = 64 * 1024


def register(bp):
    bp.add_url_rule(
        "/integrations/seerr/requests/<int:request_id>/retry",
        view_func=seerr_retry,
        methods=["POST"],
    )
    bp.add_url_rule(
        "/integrations/seerr/requests/<int:request_id>/review",
        view_func=seerr_review,
        methods=["POST"],
    )
    bp.add_url_rule(
        "/integrations/seerr/webhook", view_func=seerr_webhook, methods=["POST"]
    )
    bp.add_url_rule("/integrations/seerr/status", view_func=seerr_status)
    bp.add_url_rule("/integrations/seerr/test", view_func=seerr_test, methods=["POST"])
    bp.add_url_rule("/integrations/seerr/requests", view_func=seerr_requests)


def seerr_webhook():
    if not seerr.enabled():
        return jsonify(error="Seerr integration is disabled"), 503
    if not seerr.authenticated(request.headers.get("Authorization", "")):
        return jsonify(error="Invalid webhook credentials"), 401
    if request.mimetype != "application/json":
        return jsonify(error="Content-Type must be application/json"), 415
    request.max_content_length = MAX_PAYLOAD_BYTES
    if (
        request.content_length is not None
        and request.content_length > MAX_PAYLOAD_BYTES
    ):
        return jsonify(error="Webhook payload is too large"), 413
    try:
        payload = seerr.parse_payload(request.get_json())
    except (BadRequest, ValueError, TypeError):
        return jsonify(error="Invalid Seerr webhook payload"), 400
    if payload["event"] == "TEST_NOTIFICATION":
        return jsonify(ok=True, test=True)
    if payload.get("ignored"):
        return jsonify(ok=True, ignored=True)
    try:
        queue_id, created = seerr.accept(payload)
    except ValueError:
        return jsonify(error="Request ID conflicts with existing media or seasons"), 409
    return jsonify(
        ok=True, queue_id=queue_id, duplicate=not created
    ), 202 if created else 200


def seerr_status():
    from .. import media_targets, tmdb

    config = seerr.settings()
    targets = media_targets.read()
    return jsonify(
        **config,
        tmdb_configured=tmdb.configured(),
        targets_configured=targets["enabled"],
    )


def seerr_requests():
    try:
        offset = int(request.args.get("offset", "0"))
        limit = int(request.args.get("limit", "50"))
        if offset < 0 or offset > 10000000 or not 1 <= limit <= 200:
            raise ValueError("Invalid page")
    except ValueError:
        return jsonify(
            error="Invalid pagination; limit must be 1–200 and offset nonnegative"
        ), 400
    rows = seerr.list_requests(offset=offset, limit=limit + 1)
    return jsonify(
        items=rows[:limit], next_offset=offset + limit if len(rows) > limit else None
    )


def seerr_test():
    """Local configuration validation; Seerr Test Notification proves reachability."""
    from .. import media_targets, tmdb

    checks = {
        "enabled": seerr.enabled(),
        "secret": seerr.settings()["secret_set"],
        "tmdb": tmdb.configured(),
    }
    for kind in ("movie", "series"):
        try:
            checks[kind + "_target"] = media_targets.resolve(kind) is not None
        except (ValueError, OSError):
            checks[kind + "_target"] = False
    return jsonify(
        ok=all(checks.values()),
        checks=checks,
        message="Local configuration checked. Send Test Notification from Seerr to verify network reachability.",
    )


def seerr_review(request_id):
    try:
        queue_id = seerr.review_request(request_id, request.get_json(silent=True))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, queue_id=queue_id)


def seerr_retry(request_id):
    try:
        queue_id = seerr.retry_request(request_id)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, queue_id=queue_id)
