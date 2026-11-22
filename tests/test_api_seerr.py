import json

import pytest

from h0melab.web import db, seerr, settings_store

URL = "/api/integrations/seerr/webhook"
SECRET = "test-secret-with-at-least-32-characters"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv(seerr.ENABLED, "1")
    monkeypatch.setenv(seerr.SECRET, SECRET)
    return {"Authorization": "Bearer " + SECRET}


@pytest.fixture
def payload():
    return {
        "notification_type": "MEDIA_APPROVED",
        "media_type": "movie",
        "media_tmdbid": "157336",
        "request_id": "12",
    }


def test_auth_enabled_accepts_webhook_without_session_or_admin(
    auth_client, configured, payload
):
    response = auth_client.post(URL, json=payload, headers=configured)
    assert response.status_code == 202
    assert response.json["queue_id"]


def test_default_disabled(client, payload):
    assert client.post(URL, json=payload).status_code == 503


@pytest.mark.parametrize(
    "header", [{}, {"Authorization": "Bearer wrong"}, {"X-API-Key": SECRET}]
)
def test_bad_credentials_never_queue(client, configured, payload, header):
    assert client.post(URL, json=payload, headers=header).status_code == 401
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM download_queue").fetchone()[0] == 0


def test_general_admin_key_not_webhook_secret(client, api_key, configured, payload):
    raw, _ = api_key(scope="admin")
    assert (
        client.post(
            URL, json=payload, headers={"Authorization": "Bearer " + raw}
        ).status_code
        == 401
    )


def test_webhook_secret_not_general_admin_key(client, configured):
    assert client.get("/api/settings", headers=configured).status_code == 401


def test_test_notification_never_downloads(client, configured):
    assert client.post(
        URL, json={"notification_type": "TEST_NOTIFICATION"}, headers=configured
    ).json["test"]
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM download_queue").fetchone()[0] == 0


def test_duplicate_and_conflict(client, configured, payload):
    first = client.post(URL, json=payload, headers=configured)
    again = client.post(URL, json=payload, headers=configured)
    assert (
        first.status_code == 202
        and again.status_code == 200
        and again.json["duplicate"]
    )
    payload["media_tmdbid"] = "1"
    assert client.post(URL, json=payload, headers=configured).status_code == 409


@pytest.mark.parametrize("value", [[], None, {"notification_type": "MEDIA_APPROVED"}])
def test_bad_shapes(client, configured, value):
    assert (
        client.post(
            URL,
            data=json.dumps(value),
            content_type="application/json",
            headers=configured,
        ).status_code
        == 400
    )


def test_size_limit(client, configured):
    assert (
        client.post(URL, json={"padding": "x" * 65536}, headers=configured).status_code
        == 413
    )


def test_json_required(client, configured):
    assert client.post(URL, data="hello", headers=configured).status_code == 415


def test_secret_persisted_masked_and_not_exported(client, monkeypatch, tmp_path):
    from dotenv import dotenv_values

    destination = tmp_path / "settings.env"
    monkeypatch.setattr(settings_store, "PANEL_SETTINGS_PATH", destination)
    response = client.put(
        "/api/settings", json={"seerr": {"enabled": True, "webhook_secret": SECRET}}
    )
    assert response.status_code == 200
    assert response.json["seerr"]["secret_set"]
    assert dotenv_values(destination)[seerr.SECRET] == SECRET
    assert SECRET not in client.get("/api/settings").text
    assert SECRET not in client.get("/api/settings/env").text


@pytest.mark.parametrize("value", [True, "short", "x" * 257, "x " * 20])
def test_invalid_secret_rejected(client, value):
    assert (
        client.put(
            "/api/settings", json={"seerr": {"webhook_secret": value}}
        ).status_code
        == 400
    )


def test_secret_required_before_enable(client):
    assert (
        client.put("/api/settings", json={"seerr": {"enabled": True}}).status_code
        == 400
    )


def test_admin_scope_required(client, api_key):
    raw, _ = api_key(scope="read")
    for path in ("status", "requests"):
        assert (
            client.get(
                "/api/integrations/seerr/" + path, headers={"X-API-Key": raw}
            ).status_code
            == 403
        )


def test_manual_review_requeues_only_stored_candidate(client, configured, payload):
    qid = client.post(URL, json=payload, headers=configured).json["queue_id"]
    candidate = {"url": "https://example.test/movie", "assessment": {"conflict": False}}
    seerr.update(12, status="manual_review", candidates=[candidate])
    with db.session() as conn:
        conn.execute("UPDATE download_queue SET pause_requested=1 WHERE id=?", (qid,))
    response = client.post(
        "/api/integrations/seerr/requests/12/review",
        json={"selected_url": candidate["url"], "reason": "Verified movie identity"},
    )
    assert response.status_code == 200
    assert (
        seerr.get_request(request_id=12)["review_reason"] == "Verified movie identity"
    )
    assert db.get_queue_item(qid)["pause_requested"] == 0


def test_review_never_approves_id_conflict(client, configured, payload):
    client.post(URL, json=payload, headers=configured)
    candidate = {"url": "https://example.test/movie", "assessment": {"conflict": True}}
    seerr.update(12, status="manual_review", candidates=[candidate])
    assert (
        client.post(
            "/api/integrations/seerr/requests/12/review",
            json={"selected_url": candidate["url"], "reason": "Attempted override"},
        ).status_code
        == 400
    )


def test_review_requires_admin_scope(client, api_key):
    raw, _ = api_key(scope="write")
    assert (
        client.post(
            "/api/integrations/seerr/requests/12/review",
            json={},
            headers={"X-API-Key": raw},
        ).status_code
        == 403
    )


def test_retry_metadata_failure_without_candidates(client, configured, payload):
    qid = client.post(URL, json=payload, headers=configured).json["queue_id"]
    seerr.update(12, status="manual_review", identity_status="metadata_failed")
    with db.session() as conn:
        conn.execute("UPDATE download_queue SET pause_requested=1 WHERE id=?", (qid,))
    assert (
        client.post("/api/integrations/seerr/requests/12/retry", json={}).status_code
        == 200
    )
    assert seerr.get_request(request_id=12)["selected_url"] is None
    assert not db.get_queue_item(qid)["pause_requested"]


def test_retry_never_restarts_running_request(client, configured, payload):
    qid = client.post(URL, json=payload, headers=configured).json["queue_id"]
    db.set_queue_status(qid, "running")
    assert (
        client.post("/api/integrations/seerr/requests/12/retry", json={}).status_code
        == 400
    )


def test_seerr_pages_render_and_use_separate_integration_controls(client):
    assert client.get("/seerr").status_code == 200
    page = client.get("/settings")
    assert page.status_code == 200
    assert "seerrEndpoint" in page.text
    assert "discordMode" not in page.text


def test_contradictory_nested_seasons_never_enqueue(client, configured, payload):
    payload.update(media_type="tv", seasons=[1], request={"seasons": [2]})
    assert client.post(URL, json=payload, headers=configured).status_code == 400
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM download_queue").fetchone()[0] == 0


@pytest.mark.parametrize(
    "query", ["offset=-1", "offset=no", "limit=0", "limit=201", "limit=1.5"]
)
def test_request_pagination_rejects_invalid_bounds(client, query):
    assert client.get("/api/integrations/seerr/requests?" + query).status_code == 400


def test_request_pagination_reaches_older_manual_reviews(client):
    with db.session() as conn:
        seerr.schema(conn)
        for rid in range(1, 206):
            conn.execute(
                "INSERT INTO seerr_requests(request_id,payload,created,updated,status) VALUES (?,?,?,?,?)",
                (
                    rid,
                    json.dumps({"title": str(rid), "seasons": []}),
                    100,
                    100,
                    "manual_review" if rid == 1 else "completed",
                ),
            )
    seen = []
    offset = 0
    while offset is not None:
        response = client.get(
            f"/api/integrations/seerr/requests?offset={offset}&limit=50"
        )
        assert response.status_code == 200
        assert len(response.json["items"]) <= 50
        seen.extend(row["request_id"] for row in response.json["items"])
        offset = response.json["next_offset"]
    assert seen == list(range(205, 0, -1))
    assert response.json["items"][-1]["status"] == "manual_review"


@pytest.mark.parametrize("current,total", [(0, 0), (1048576, 0), (1048576, 2097152)])
def test_request_history_exposes_current_transfer_bytes(
    client, configured, payload, current, total
):
    accepted = client.post(URL, json=payload, headers=configured)
    qid = accepted.json["queue_id"]
    db.update_queue_bytes(qid, current, total)
    response = client.get("/api/integrations/seerr/requests")
    assert response.status_code == 200
    item = next(row for row in response.json["items"] if row["queue_id"] == qid)
    assert item["queue"]["current_bytes"] == current
    assert item["queue"]["total_bytes"] == total


@pytest.mark.parametrize("event", ["MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"])
@pytest.mark.parametrize("kind", ["movie", "tv"])
@pytest.mark.parametrize("imdb", ["", "tt1234567"])
def test_readme_webhook_template_contract(client, configured, event, kind, imdb):
    import re
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    templates = [
        json.loads(block)
        for block in re.findall(r"```json\n(.*?)\n```", readme, re.DOTALL)
        if '"{{extra}}"' in block
    ]
    assert len(templates) == 1
    values = {
        "notification_type": event,
        "subject": 'Example "Quoted"',
        "media_type": kind,
        "media_tmdbid": "42",
        "media_imdbid": imdb,
        "media_tvdbid": "",
        "request_id": "987",
        "requestedBy_username": "Test",
    }
    # Seerr parses JSON first, then replaces string values and the special extra key.
    rendered = {}
    for key, value in templates[0].items():
        if key == "{{extra}}":
            rendered["extra"] = (
                [{"name": "Requested Seasons", "value": "1, 2"}] if kind == "tv" else []
            )
        else:
            assert isinstance(value, str)
            for name, replacement in values.items():
                value = value.replace("{{" + name + "}}", replacement, 1)
            assert "{{" not in value
            rendered[key] = value
    response = client.post(URL, json=rendered, headers=configured)
    assert response.status_code == 202
    stored = seerr.get_request(request_id=987)["payload"]
    assert stored["event"] == event
    assert stored["media_type"] == kind and stored["tmdb_id"] == 42
    assert stored["imdb_id"] == (imdb or None)
    assert stored["seasons"] == ([1, 2] if kind == "tv" else [])
    assert stored["title"] == values["subject"]
