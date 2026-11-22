"""Seerr request parsing and durable metadata, sharing the existing download queue."""

import json
import os
import re
import secrets
import time
from datetime import date

from . import db

ENABLED = "H0MELAB_SEERR_ENABLED"
SECRET = "H0MELAB_SEERR_WEBHOOK_SECRET"
APPROVED = {"MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"}


def enabled():
    return os.environ.get(ENABLED, "0") == "1"


def authenticated(header):
    secret = os.environ.get(SECRET, "").strip()
    provided = header.removeprefix("Bearer ")
    return bool(secret) and secrets.compare_digest(provided.encode(), secret.encode())


def settings():
    return {
        "enabled": enabled(),
        "secret_set": bool(os.environ.get(SECRET, "").strip()),
        "endpoint": "/api/integrations/seerr/webhook",
    }


def collect_settings(payload, updates):
    from .settings_store import SECRET_PLACEHOLDER, SettingsError

    if not isinstance(payload, dict):
        raise SettingsError("seerr must be an object")
    if "enabled" in payload:
        if not isinstance(payload["enabled"], bool):
            raise SettingsError("seerr.enabled must be a boolean")
        updates[ENABLED] = "1" if payload["enabled"] else "0"
    if "webhook_secret" in payload:
        secret = payload["webhook_secret"]
        if not isinstance(secret, str):
            raise SettingsError("Seerr secret must be a string")
        if secret != SECRET_PLACEHOLDER:
            if secret and (
                len(secret) < 32
                or len(secret) > 256
                or not secret.isascii()
                or any(c.isspace() for c in secret)
            ):
                raise SettingsError(
                    "Use 32–256 non-whitespace ASCII characters for the Seerr secret"
                )
            updates[SECRET] = secret
    if updates.get(ENABLED, os.environ.get(ENABLED)) == "1" and not updates.get(
        SECRET, os.environ.get(SECRET, "")
    ):
        raise SettingsError("Configure a Seerr webhook secret before enabling")


def _number(value, label, minimum=1):
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,10}", str(value)):
        raise ValueError(f"Invalid {label}")
    result = int(value)
    if result < minimum or result > 2147483647:
        raise ValueError(f"Invalid {label}")
    return result


def _pick(data, nested, *keys):
    values = [
        obj[k] for obj in (data, nested) for k in keys if obj.get(k) not in (None, "")
    ]
    if len({str(v) for v in values}) > 1:
        raise ValueError(f"Conflicting {keys[0]} values")
    return values[0] if values else None


def parse_payload(data):
    if not isinstance(data, dict):
        raise ValueError("Webhook payload must be an object")
    event = data.get("notification_type")
    if not isinstance(event, str):
        raise ValueError("notification_type is required")
    if event == "TEST_NOTIFICATION":
        return {"event": event}
    if event not in APPROVED:
        return {"event": event, "ignored": True}
    media, request = data.get("media") or {}, data.get("request") or {}
    if not isinstance(media, dict) or not isinstance(request, dict):
        raise ValueError("media and request must be objects")
    kind = _pick(data, media, "media_type", "mediaType")
    if kind not in ("movie", "tv"):
        raise ValueError("media_type must be movie or tv")
    rid = _number(_pick(data, request, "request_id", "id"), "request_id")
    tid = _pick(data, media, "media_tmdbid", "tmdbId", "tmdb_id")
    imdb = _pick(data, media, "media_imdbid", "imdbId", "imdb_id")
    if tid not in (None, ""):
        tid = _number(tid, "TMDB ID")
    if imdb is not None and (
        not isinstance(imdb, str) or not re.fullmatch(r"tt\d{7,10}", imdb)
    ):
        raise ValueError("Invalid IMDb ID")
    if not tid and not imdb:
        raise ValueError("A TMDB or IMDb ID is required")
    # Explicit contradictory request state never overrides approval semantics.
    if request.get("status") not in (None, 2, "2", "APPROVED"):
        raise ValueError("Request status contradicts approval notification")
    season_values = [
        obj["seasons"] for obj in (data, request) if obj.get("seasons") is not None
    ]
    extra = data.get("extra", [])
    if not isinstance(extra, list) or len(extra) > 100:
        raise ValueError("extra must be a bounded list")
    for item in extra:
        if not isinstance(item, dict):
            raise ValueError("extra entries must be objects")
        if item.get("name") in ("Requested Seasons", "Seasons"):
            if item.get("value") is not None:
                season_values.append(item["value"])

    def numbers(raw):
        if raw is None:
            return []
        if isinstance(raw, str):
            raw = raw.split(",")
        if not isinstance(raw, list) or len(raw) > 100:
            raise ValueError("Requested seasons must be an explicit list")
        return sorted(
            {
                _number(
                    _pick(v, {}, "seasonNumber", "season_number")
                    if isinstance(v, dict)
                    else str(v).strip(),
                    "season",
                    0,
                )
                for v in raw
            }
        )

    normalized = [numbers(value) for value in season_values]
    if normalized and any(value != normalized[0] for value in normalized[1:]):
        raise ValueError("Conflicting requested seasons")
    requested = normalized[0] if normalized else []
    tvdb = _pick(data, media, "media_tvdbid", "tvdbId", "tvdb_id")
    original_title = _pick(data, media, "original_title", "originalTitle")
    if original_title is not None and (
        not isinstance(original_title, str) or len(original_title) > 500
    ):
        raise ValueError("Invalid original title")
    year = _pick(data, media, "release_year", "releaseYear", "year")
    if year is not None:
        year = _number(year, "release year", 1800)
        if year > 9999:
            raise ValueError("Invalid release year")
    release_date = _pick(data, media, "release_date", "releaseDate")
    if release_date is not None:
        if not isinstance(release_date, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", release_date
        ):
            raise ValueError("Invalid release date")
        parsed_date = date.fromisoformat(release_date)
        if year is not None and parsed_date.year != year:
            raise ValueError("Release year and date conflict")
    return {
        "event": event,
        "request_id": rid,
        "media_type": kind,
        "tmdb_id": tid,
        "imdb_id": imdb,
        "tvdb_id": _number(tvdb, "TVDB ID") if tvdb else None,
        "seasons": requested if kind == "tv" else [],
        "original_title": original_title,
        "release_year": year,
        "release_date": release_date,
        "title": str(data.get("subject") or data.get("title") or "")[:300],
        "requested_by": str(
            data.get("requestedBy_username")
            or request.get("requestedBy_username")
            or ""
        )[:200],
    }


def schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS seerr_requests (
        request_id INTEGER PRIMARY KEY, queue_id INTEGER, payload TEXT NOT NULL,
        identity TEXT, candidates TEXT NOT NULL DEFAULT '[]', selected_url TEXT,
        status TEXT NOT NULL DEFAULT 'received', identity_status TEXT NOT NULL DEFAULT 'pending',
        error TEXT NOT NULL DEFAULT '', target_path TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL, updated REAL NOT NULL)""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(seerr_requests)")}
    for name, definition in (
        ("episode_mapping", "TEXT NOT NULL DEFAULT '[]'"),
        ("episode_review", "TEXT NOT NULL DEFAULT '{}'"),
        ("review_reason", "TEXT NOT NULL DEFAULT ''"),
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE seerr_requests ADD COLUMN {name} {definition}")
    conn.execute("""CREATE TABLE IF NOT EXISTS seerr_media (
        media_type TEXT NOT NULL, tmdb_id INTEGER NOT NULL, imdb_id TEXT,
        folder TEXT NOT NULL, UNIQUE(media_type,tmdb_id), UNIQUE(media_type,folder))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS seerr_files (
        media_type TEXT NOT NULL, tmdb_id INTEGER NOT NULL, season INTEGER NOT NULL,
        episode INTEGER NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL, PRIMARY KEY(media_type,tmdb_id,season,episode,path))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS seerr_events (
        request_id INTEGER NOT NULL, event TEXT NOT NULL, PRIMARY KEY(request_id,event))""")


def accept(payload):
    """Request and placeholder are committed together; no network in HTTP request."""
    now = time.time()
    with db.session() as conn:
        schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute(
            "SELECT * FROM seerr_requests WHERE request_id=?", (payload["request_id"],)
        ).fetchone()
        if old:
            prior = json.loads(old["payload"])
            for field in ("media_type", "tmdb_id", "imdb_id", "seasons"):
                if prior[field] != payload[field]:
                    raise ValueError(
                        "Request ID already refers to different media or seasons"
                    )
            return old["queue_id"], False
        cur = conn.execute(
            """INSERT INTO download_queue
            (title,series_url,episodes,total_episodes,language,provider,source,payload_version,request_key)
            VALUES (?,?,'[]',0,?,?,'seerr',3,?)""",
            (
                payload["title"] or f"Seerr #{payload['request_id']}",
                "",
                os.environ.get("H0MELAB_LANGUAGE", "German Dub"),
                os.environ.get("H0MELAB_PROVIDER", "VOE") or "VOE",
                f"seerr:{payload['request_id']}",
            ),
        )
        qid = cur.lastrowid
        conn.execute("UPDATE download_queue SET position=? WHERE id=?", (qid, qid))
        conn.execute(
            "INSERT INTO seerr_requests(request_id,queue_id,payload,created,updated) VALUES (?,?,?,?,?)",
            (payload["request_id"], qid, json.dumps(payload), now, now),
        )
    return qid, True


def get_request(queue_id=None, request_id=None):
    with db.session() as conn:
        schema(conn)
        field, value = (
            ("queue_id", queue_id)
            if queue_id is not None
            else ("request_id", request_id)
        )
        row = conn.execute(
            f"SELECT * FROM seerr_requests WHERE {field}=?", (value,)
        ).fetchone()
    if not row:
        raise ValueError("Seerr request not found")
    result = dict(row)
    for key in (
        "payload",
        "identity",
        "candidates",
        "episode_mapping",
        "episode_review",
    ):
        result[key] = json.loads(result[key]) if result[key] else None
    return result


def update(request_id, **fields):
    allowed = {
        "identity",
        "candidates",
        "selected_url",
        "status",
        "identity_status",
        "error",
        "target_path",
        "queue_id",
        "episode_mapping",
        "episode_review",
        "review_reason",
        "retry_count",
    }
    if not fields or set(fields) - allowed:
        raise ValueError("Invalid request fields")
    fields = {
        k: json.dumps(v)
        if k in ("identity", "candidates", "episode_mapping", "episode_review")
        else v
        for k, v in fields.items()
    }
    fields["updated"] = time.time()
    with db.session() as conn:
        schema(conn)
        conn.execute(
            "UPDATE seerr_requests SET "
            + ",".join(k + "=?" for k in fields)
            + " WHERE request_id=?",
            (*fields.values(), request_id),
        )


def list_requests(offset=0, limit=200):
    queue_fields = (
        "status",
        "current_episode",
        "total_episodes",
        "current_bytes",
        "total_bytes",
        "errors",
        "not_before",
    )
    queue_columns = ",".join(f"q.{name} AS queue_{name}" for name in queue_fields)
    with db.session() as conn:
        schema(conn)
        rows = conn.execute(
            f"SELECT r.*,q.id AS joined_queue_id,{queue_columns} FROM seerr_requests r "
            "LEFT JOIN download_queue q ON q.id=r.queue_id "
            "ORDER BY r.created DESC,r.request_id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    results = []
    for stored in rows:
        row = dict(stored)
        joined = row.pop("joined_queue_id")
        queue = {name: row.pop("queue_" + name) for name in queue_fields}
        for name in (
            "payload",
            "identity",
            "candidates",
            "episode_mapping",
            "episode_review",
        ):
            row[name] = json.loads(row[name]) if row[name] else None
        row["queue"] = queue if joined is not None else None
        results.append(row)
    return results


def notify(request_id, event, title):
    """Persist deduplication before best-effort transport; webhook replay stays quiet."""
    with db.session() as conn:
        schema(conn)
        inserted = conn.execute(
            "INSERT OR IGNORE INTO seerr_events VALUES (?,?)", (request_id, event)
        ).rowcount
    if inserted:
        from .discord_bot import notify_event

        notify_event(title, event)


def prepare(item):
    from .seerr_processing import prepare as process

    return process(item)


def review_request(request_id, data):
    """An administrator chooses evidence; the worker must verify it afresh."""
    if not isinstance(data, dict):
        raise ValueError("Review must be an object")
    selected = data.get("selected_url")
    reason = data.get("reason", "")
    mapping = data.get("episode_mapping", [])
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 2000:
        raise ValueError("Provide a review reason (5–2000 characters)")
    if not isinstance(mapping, list) or len(mapping) > 2000:
        raise ValueError("Invalid episode mapping")
    seen, urls = set(), set()
    clean = []
    for row in mapping:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("url"), str)
            or len(row["url"]) > 4096
        ):
            raise ValueError("Invalid episode mapping")
        season = _number(row.get("season_number"), "season", 0)
        episode = _number(row.get("episode_number"), "episode")
        if (season, episode) in seen or row["url"] in urls:
            raise ValueError("Episode mappings must be one-to-one")
        seen.add((season, episode))
        urls.add(row["url"])
        entry = {"url": row["url"], "season_number": season, "episode_number": episode}
        if "source_season_number" in row:
            entry["source_season_number"] = _number(
                row["source_season_number"], "source season", 0
            )
        clean.append(entry)
    with db.session() as conn:
        schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM seerr_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Request not found")
        queue = conn.execute(
            "SELECT * FROM download_queue WHERE id=?", (row["queue_id"],)
        ).fetchone()
        if (
            queue is None
            or queue["status"] == "running"
            or row["status"] in ("completed", "already_present")
        ):
            raise ValueError("Request is not available for review")
        if row["status"] not in ("manual_review", "partial") and queue[
            "status"
        ] not in (
            "failed",
            "cancelled",
        ):
            raise ValueError("Request must be stopped before review")
        hits = json.loads(row["candidates"])
        if not isinstance(selected, str) or not any(
            h.get("url") == selected for h in hits
        ):
            raise ValueError("Select a stored provider candidate")
        hit = next(h for h in hits if h.get("url") == selected)
        if hit.get("assessment", {}).get("conflict") or hit.get("metadata_conflicts"):
            raise ValueError("Conflicting media identities cannot be approved")
        payload = json.loads(row["payload"])
        if any(m["season_number"] not in payload["seasons"] for m in clean):
            raise ValueError("Mapping includes an unrequested season")
        conn.execute(
            """UPDATE seerr_requests SET selected_url=?,episode_mapping=?,review_reason=?,
            status='waiting',error='',retry_count=0,updated=? WHERE request_id=?""",
            (selected, json.dumps(clean), reason.strip(), time.time(), request_id),
        )
        conn.execute(
            """UPDATE download_queue SET status='queued',pause_requested=0,
            cancel_requested=0,force_cancelled=0,errors='[]',started_at=NULL,completed_at=NULL,
            current_episode=0,not_before=0 WHERE id=?""",
            (row["queue_id"],),
        )
    return row["queue_id"]


def retry_request(request_id):
    """Recheck metadata after transient failures, without inventing a selection."""
    with db.session() as conn:
        schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM seerr_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Request not found")
        queue = conn.execute(
            "SELECT * FROM download_queue WHERE id=?", (row["queue_id"],)
        ).fetchone()
        if (
            queue is None
            or queue["status"] == "running"
            or row["status"] in ("completed", "already_present")
        ):
            raise ValueError("Request is not available for retry")
        if row["status"] not in ("manual_review", "partial") and queue[
            "status"
        ] not in (
            "failed",
            "cancelled",
        ):
            raise ValueError("Request must be stopped before retry")
        conn.execute(
            "UPDATE seerr_requests SET status='waiting',error='',retry_count=0,updated=? WHERE request_id=?",
            (time.time(), request_id),
        )
        conn.execute(
            """UPDATE download_queue SET status='queued',pause_requested=0,
            cancel_requested=0,force_cancelled=0,errors='[]',started_at=NULL,completed_at=NULL,
            current_episode=0,not_before=0 WHERE id=?""",
            (row["queue_id"],),
        )
    return row["queue_id"]
