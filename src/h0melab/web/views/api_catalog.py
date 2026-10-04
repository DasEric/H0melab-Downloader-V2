"""Catalog API for eBooks, audio books and podcasts."""

import random

from flask import Response, current_app, jsonify, request, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ...catalog import get_source, source_summaries
from ...catalog.common import CatalogError, ensure_public_url, get
from ...models.common.library_layout import LibraryItem, media_path
from .. import db, settings_store, worker

_COVER_TYPES = {"image/avif", "image/gif", "image/jpeg", "image/png", "image/webp"}


def register(bp):
    bp.add_url_rule("/catalog/sources", view_func=catalog_sources)
    bp.add_url_rule("/catalog/browse", view_func=catalog_browse)
    bp.add_url_rule("/catalog/random", view_func=catalog_random)
    bp.add_url_rule("/catalog/search", view_func=catalog_search)
    bp.add_url_rule("/catalog/details", view_func=catalog_details)
    bp.add_url_rule("/catalog/cover", view_func=catalog_cover)
    bp.add_url_rule("/catalog/download", view_func=catalog_download, methods=["POST"])


def _username():
    if not current_app.config.get("AUTH_ENABLED", False):
        return None
    from ..auth import current_username

    return current_username()


def catalog_sources():
    return jsonify(
        {
            "sources": source_summaries(),
            "library_paths": settings_store.media_library_paths(),
        }
    )


def _cover_serializer():
    from ..auth import get_or_create_secret_key

    serializer = current_app.extensions.get("catalog_cover_serializer")
    if serializer is not None:
        return serializer
    key = current_app.secret_key or get_or_create_secret_key()
    serializer = URLSafeTimedSerializer(key, salt="catalog-cover")
    current_app.extensions["catalog_cover_serializer"] = serializer
    return serializer


def _proxy_cover(data):
    cover_url = data.get("cover_url") or ""
    if cover_url:
        token = _cover_serializer().dumps(cover_url)
        data["cover_url"] = url_for("api.catalog_cover", token=token)
    return data


def catalog_cover():
    token = (request.args.get("token") or "").strip()
    try:
        target = _cover_serializer().loads(token, max_age=3600)
        ensure_public_url(target)
        upstream = get(
            target, accept="image/avif,image/webp,image/png,image/jpeg,image/*"
        )
    except (BadSignature, SignatureExpired, CatalogError, ValueError):
        return "", 400
    content_type = (upstream.headers.get("content-type") or "").split(";", 1)[0].lower()
    content = upstream.content
    close = getattr(upstream, "close", None)
    if close:
        close()
    if content_type not in _COVER_TYPES or len(content) > 12 * 1024 * 1024:
        return "", 502
    return Response(
        content,
        content_type=content_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _source():
    return get_source((request.args.get("source") or "").strip())


def _catalog_error(exc):
    return jsonify({"error": str(exc), "error_code": "catalog_unavailable"}), 400


def catalog_browse():
    try:
        page = max(1, min(int(request.args.get("page", 1)), 100))
        found = _source().browse(page=page)
    except (CatalogError, ValueError) as exc:
        return _catalog_error(exc)
    return jsonify(
        {"items": [_proxy_cover(item.as_dict()) for item in found], "page": page}
    )


def catalog_random():
    try:
        source = _source()
        page = random.randint(1, 3)
        found = source.browse(page=page)
        if not found and page != 1:
            found = source.browse(page=1)
        if not found:
            raise CatalogError("The catalog returned no entries")
    except (CatalogError, ValueError) as exc:
        return _catalog_error(exc)
    return jsonify({"item": _proxy_cover(random.choice(found).as_dict())})


def catalog_search():
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify(
            {"error": "Search query must contain at least two characters"}
        ), 400
    try:
        page = max(1, min(int(request.args.get("page", 1)), 100))
        found = _source().search(query, page=page)
    except (CatalogError, ValueError) as exc:
        return _catalog_error(exc)
    return jsonify(
        {"items": [_proxy_cover(item.as_dict()) for item in found], "page": page}
    )


def _target_for(item, asset):
    preview = LibraryItem(
        item.media_kind,
        item.title,
        item.author,
        item.year,
        item.language,
        asset.extension,
        asset.title,
        asset.published,
        asset.track,
    )
    return str(media_path(settings_store.media_library_path(item.media_kind), preview))


def _with_target(item):
    data = _proxy_cover(item.as_dict())
    if item.assets:
        targets = {asset.id: _target_for(item, asset) for asset in item.assets}
        for asset in data["assets"]:
            asset["target_path"] = targets[asset["id"]]
        data["target_path"] = targets[item.assets[0].id]
    return data


def catalog_details():
    item_id = (request.args.get("id") or "").strip()
    if not item_id:
        return jsonify({"error": "id is required"}), 400
    try:
        item = _source().details(item_id)
    except (CatalogError, ValueError) as exc:
        return _catalog_error(exc)
    return jsonify(_with_target(item))


def catalog_download():
    data = request.get_json(silent=True) or {}
    source_key = str(data.get("source") or "").strip()
    item_id = str(data.get("id") or "").strip()
    asset_id = str(data.get("asset_id") or "").strip()
    if not source_key or not item_id or not asset_id:
        return jsonify({"error": "source, id and asset_id are required"}), 400
    try:
        source = get_source(source_key)
        item = source.details(item_id)
        asset = next(
            (candidate for candidate in item.assets if candidate.id == asset_id), None
        )
        if asset is None:
            raise CatalogError("The selected asset is no longer available")
    except (CatalogError, ValueError) as exc:
        return _catalog_error(exc)

    item_payload = item.as_dict()
    item_payload.update(
        episode_title=asset.title,
        episode_date=asset.published,
        track=asset.track,
    )
    entry = {"item": item_payload, "asset": asset.as_dict()}
    queue_id = db.add_to_queue(
        title=item.title,
        series_url=item.id,
        episodes=[entry],
        language=item.language or "Unknown",
        provider=f"catalog:{source.key}",
        username=_username(),
        source="catalog",
        media_kind=item.media_kind,
        source_site=source.key,
        transfer_kind="http",
        payload_version=1,
    )
    worker.ensure_started()
    return jsonify({"queue_id": queue_id, "target_path": _target_for(item, asset)})
