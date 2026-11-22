import json

import pytest

from h0melab.web import db, seerr


@pytest.fixture
def payload():
    return {
        "notification_type": "MEDIA_APPROVED",
        "media_type": "movie",
        "media_tmdbid": "157336",
        "media_imdbid": "tt0816692",
        "request_id": "5",
        "subject": "Interstellar",
    }


@pytest.mark.parametrize(
    "event", ["MEDIA_PENDING", "MEDIA_DECLINED", "MEDIA_AVAILABLE"]
)
def test_unapproved_events_are_ignored(payload, event):
    payload["notification_type"] = event
    assert seerr.parse_payload(payload)["ignored"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("media_type", "series"),
        ("media_type", "unknown"),
        ("media_tmdbid", True),
        ("media_tmdbid", -1),
        ("media_imdbid", "not-an-id"),
        ("request_id", True),
    ],
)
def test_payload_rejects_invalid_identity(payload, field, value):
    payload[field] = value
    with pytest.raises(ValueError):
        seerr.parse_payload(payload)


def test_nested_official_payload():
    result = seerr.parse_payload(
        {
            "notification_type": "MEDIA_AUTO_APPROVED",
            "media": {"media_type": "tv", "tmdbId": "1405", "imdbId": "tt0773262"},
            "request": {"request_id": "8"},
            "extra": [{"name": "Requested Seasons", "value": "1, 2"}],
        }
    )
    assert result["seasons"] == [1, 2]
    assert result["media_type"] == "tv"


def test_season_contradiction(payload):
    payload.update(
        media_type="tv",
        seasons=[1],
        extra=[{"name": "Requested Seasons", "value": "2"}],
    )
    with pytest.raises(ValueError):
        seerr.parse_payload(payload)


def test_queue_request_atomic_idempotency(payload):
    parsed = seerr.parse_payload(payload)
    qid, created = seerr.accept(parsed)
    assert created
    assert seerr.accept(parsed) == (qid, False)
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM download_queue").fetchone()[0] == 1
    record = seerr.get_request(queue_id=qid)
    assert record["payload"]["tmdb_id"] == 157336


def test_request_identity_cannot_be_replaced(payload):
    parsed = seerr.parse_payload(payload)
    seerr.accept(parsed)
    with pytest.raises(ValueError):
        seerr.accept({**parsed, "tmdb_id": 1})


def test_request_id_survives_queue_cleanup(payload):
    parsed = seerr.parse_payload(payload)
    qid, _ = seerr.accept(parsed)
    with db.session() as conn:
        conn.execute("DELETE FROM download_queue WHERE id=?", (qid,))
    assert seerr.accept(parsed) == (qid, False)


def test_secret_check(monkeypatch):
    monkeypatch.setenv(seerr.SECRET, "x" * 32)
    assert seerr.authenticated("Bearer " + "x" * 32)
    assert seerr.authenticated("x" * 32)
    assert not seerr.authenticated("Bearer " + "y" * 32)


def test_secret_not_exposed(monkeypatch):
    monkeypatch.setenv(seerr.SECRET, "x" * 32)
    assert "x" * 32 not in json.dumps(seerr.settings())


@pytest.mark.parametrize(
    "fields",
    [
        {"seasons": [1], "request": {"seasons": [2]}},
        {"seasons": [], "extra": [{"name": "Requested Seasons", "value": "1"}]},
        {
            "extra": [
                {"name": "Requested Seasons", "value": "1"},
                {"name": "Seasons", "value": "2"},
            ]
        },
        {"seasons": [{"seasonNumber": 1, "season_number": 2}]},
    ],
)
def test_all_season_representations_must_agree(payload, fields):
    payload.update(media_type="tv", **fields)
    with pytest.raises(ValueError, match="Conflicting"):
        seerr.parse_payload(payload)


def test_equivalent_season_representations_are_accepted(payload):
    payload.update(
        media_type="tv",
        seasons=[2, 1, 1],
        request={
            "seasons": [{"seasonNumber": 1, "season_number": 1}, {"seasonNumber": 2}]
        },
        extra=[
            {"name": "Requested Seasons", "value": "1, 2"},
            {"name": "Seasons", "value": "2,1"},
        ],
    )
    assert seerr.parse_payload(payload)["seasons"] == [1, 2]


def test_optional_webhook_metadata_is_validated_and_persisted(payload):
    payload["media"] = {
        "originalTitle": "Interstellar",
        "releaseYear": "2014",
        "releaseDate": "2014-11-05",
    }
    parsed = seerr.parse_payload(payload)
    qid, created = seerr.accept(parsed)
    assert created
    stored = seerr.get_request(queue_id=qid)["payload"]
    assert stored["original_title"] == "Interstellar"
    assert stored["release_year"] == 2014
    assert stored["release_date"] == "2014-11-05"


@pytest.mark.parametrize(
    "metadata",
    [
        {"original_title": []},
        {"original_title": "x" * 501},
        {"release_year": True},
        {"release_year": 10000},
        {"release_date": "2014-02-30"},
        {"release_date": "20141105"},
        {"release_year": 2015, "release_date": "2014-11-05"},
        {"original_title": "One", "media": {"originalTitle": "Two"}},
    ],
)
def test_invalid_optional_webhook_metadata_is_rejected(payload, metadata):
    payload.update(metadata)
    with pytest.raises(ValueError):
        seerr.parse_payload(payload)


def test_concurrent_event_replays_schedule_one_notification(monkeypatch, payload):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock

    from h0melab.web import discord_bot

    seerr.accept(seerr.parse_payload(payload))
    calls = []
    lock = Lock()

    def scheduled(title, event):
        with lock:
            calls.append((title, event))
        return True

    monkeypatch.setattr(discord_bot, "notify_event", scheduled)
    # Initialize once, then exercise independent SQLite connections concurrently.
    with db.session() as conn:
        seerr.schema(conn)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(lambda _: seerr.notify(5, "completed", "Interstellar"), range(16))
        )
    assert calls == [("Interstellar", "completed")]
    seerr.notify(5, "completed", "Interstellar")
    assert len(calls) == 1
    seerr.notify(5, "episode_completed:1:1", "Interstellar")
    seerr.notify(5, "episode_completed:1:2", "Interstellar")
    assert len(calls) == 3
    with db.session() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM seerr_events WHERE request_id=5"
            ).fetchone()[0]
            == 3
        )


def test_unscheduled_notification_does_not_change_request_or_queue(
    monkeypatch, payload
):
    from h0melab.web import discord_bot

    qid, _ = seerr.accept(seerr.parse_payload(payload))
    before_request = seerr.get_request(request_id=5)
    before_queue = db.get_queue_item(qid)
    calls = []
    monkeypatch.setattr(
        discord_bot, "notify_event", lambda *args: calls.append(args) or False
    )
    seerr.notify(5, "approved_request", "Interstellar")
    seerr.notify(5, "approved_request", "Interstellar")
    assert len(calls) == 1
    assert seerr.get_request(request_id=5) == before_request
    assert db.get_queue_item(qid) == before_queue


@pytest.mark.parametrize(
    "actions", [("review", "review"), ("retry", "retry"), ("review", "retry")]
)
def test_concurrent_admin_actions_only_requeue_once(payload, actions):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    qid, _ = seerr.accept(seerr.parse_payload(payload))
    url = "https://example.test/verified"
    seerr.update(
        5,
        status="manual_review",
        candidates=[{"url": url, "assessment": {"conflict": False}}],
    )
    with db.session() as conn:
        conn.execute("UPDATE download_queue SET pause_requested=1 WHERE id=?", (qid,))
    barrier = Barrier(2)

    def action(pair):
        index, kind = pair
        barrier.wait(timeout=10)
        try:
            if kind == "review":
                result = seerr.review_request(
                    5,
                    {
                        "selected_url": url,
                        "reason": f"Verified by administrator {index}",
                    },
                )
            else:
                result = seerr.retry_request(5)
            return ("accepted", result, index, kind)
        except ValueError:
            return ("rejected", None, index, kind)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(action, enumerate(actions)))
    accepted = [row for row in results if row[0] == "accepted"]
    assert len(accepted) == 1
    assert accepted[0][1] == qid
    request = seerr.get_request(request_id=5)
    assert request["status"] == "waiting"
    if accepted[0][3] == "review":
        assert request["review_reason"] == f"Verified by administrator {accepted[0][2]}"
    else:
        assert not request["review_reason"]
    item = db.claim_next_queued()
    assert item["id"] == qid
    assert db.claim_next_queued() is None


@pytest.mark.parametrize("action", ["review", "retry"])
def test_admin_action_rolls_back_when_queue_write_fails(payload, action):
    import sqlite3

    qid, _ = seerr.accept(seerr.parse_payload(payload))
    url = "https://example.test/verified"
    seerr.update(5, status="manual_review", candidates=[{"url": url}])
    with db.session() as conn:
        conn.execute("UPDATE download_queue SET pause_requested=1 WHERE id=?", (qid,))
        conn.execute(
            "CREATE TRIGGER fail_resume BEFORE UPDATE ON download_queue "
            "WHEN NEW.pause_requested=0 BEGIN SELECT RAISE(ABORT, 'injected queue failure'); END"
        )
    before_request, before_queue = (
        seerr.get_request(request_id=5),
        db.get_queue_item(qid),
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected queue failure"):
        if action == "review":
            seerr.review_request(
                5, {"selected_url": url, "reason": "Verified manually"}
            )
        else:
            seerr.retry_request(5)
    assert seerr.get_request(request_id=5) == before_request
    assert db.get_queue_item(qid) == before_queue


def test_request_page_uses_one_database_session_and_preserves_missing_queue(
    monkeypatch, payload
):
    from contextlib import contextmanager

    first = seerr.parse_payload(payload)
    first_id, _ = seerr.accept(first)
    second_id, _ = seerr.accept({**first, "request_id": 6})
    db.set_queue_status(first_id, "running")
    db.update_queue_progress(first_id, 1, "")
    with db.session() as conn:
        conn.execute("DELETE FROM download_queue WHERE id=?", (second_id,))
    original = db.session
    calls = []

    @contextmanager
    def counted():
        calls.append(1)
        with original() as connection:
            yield connection

    monkeypatch.setattr(db, "session", counted)
    rows = seerr.list_requests(limit=200)
    assert len(calls) == 1
    by_id = {row["request_id"]: row for row in rows}
    assert by_id[5]["queue"]["status"] == "running"
    assert by_id[5]["queue"]["current_episode"] == 1
    assert by_id[6]["queue"] is None
    assert by_id[5]["payload"] == first
    assert by_id[5]["episode_mapping"] == []
    assert by_id[5]["episode_review"] == {}
    assert not any(key.startswith("queue_") and key != "queue_id" for key in by_id[5])
