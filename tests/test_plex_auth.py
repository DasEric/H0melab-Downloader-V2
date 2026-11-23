from urllib.parse import parse_qs, urlsplit

import pytest

from h0melab.web import db, plex, plex_auth, settings_store


@pytest.fixture
def cloud(monkeypatch, tmp_path):
    calls = []
    state = {"claimed": False, "fail": False}
    monkeypatch.setattr(
        settings_store, "PANEL_SETTINGS_PATH", tmp_path / "settings.env"
    )

    def request(method, path, client, token=None, **kwargs):
        calls.append((method, path, client, token, kwargs))
        if state["fail"]:
            raise settings_store.SettingsError("Cloud unavailable")
        if path == "pins":
            return {"id": 42, "code": "test-pin", "expiresIn": 600}
        if path == "pins/42":
            return {
                "id": 42,
                "code": "test-pin",
                "authToken": "account-secret" if state["claimed"] else None,
            }
        assert path == "resources"
        assert token == "account-secret"
        return [
            {
                "name": "Owned server",
                "owned": True,
                "provides": "server",
                "accessToken": "server-secret",
                "connections": [
                    {"uri": "http://nas:32400", "relay": False},
                    {"uri": "https://nas.plex.direct:32400", "relay": False},
                ],
            },
            {
                "name": "Shared server",
                "owned": False,
                "provides": "server",
                "accessToken": "shared-secret",
                "connections": [{"uri": "http://shared:32400"}],
            },
            {
                "name": "Invalid URL",
                "owned": True,
                "provides": "server",
                "accessToken": "invalid-secret",
                "connections": [{"uri": "file:///etc/passwd"}],
            },
        ]

    monkeypatch.setattr(plex_auth, "_cloud", request)
    monkeypatch.setattr(
        plex,
        "libraries",
        lambda payload: [{"id": "1", "name": "Movies", "type": "movies"}],
    )
    return state, calls


def post(client, action, body=None):
    return client.post("/api/settings/plex/login/" + action, json=body or {})


def test_login_flow_does_not_expose_tokens(client, cloud):
    state, calls = cloud
    start = post(client, "start")
    assert start.status_code == 200
    cookie = start.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert start.headers["Cache-Control"] == "no-store"
    url = start.json["auth_url"]
    assert url.startswith("https://app.plex.tv/auth#?")
    assert parse_qs(urlsplit(url).fragment[1:])["code"] == ["test-pin"]
    assert post(client, "poll").json == {"status": "pending"}
    state["claimed"] = True
    result = post(client, "poll")
    assert len(result.json["servers"]) == 2
    assert "secret" not in result.get_data(as_text=True)
    assert not plex.settings()["key_set"]
    chosen = post(client, "select", {"server": "1"})
    assert chosen.status_code == 200
    assert chosen.json["url"] == "https://nas.plex.direct:32400"
    assert "secret" not in chosen.get_data(as_text=True)
    assert plex.settings()["key_set"]
    assert plex.settings()["movie_library_id"] == ""
    assert "server-secret" not in settings_store.export_env()
    assert post(client, "poll").status_code == 400
    assert calls[0][4]["data"]["strong"] == "true"


def test_cancel_and_expiry_keep_existing_settings(client, cloud, monkeypatch):
    monkeypatch.setenv(plex.KEYS["api_key"], "existing")
    post(client, "start")
    assert post(client, "cancel").json["status"] == "cancelled"
    assert post(client, "poll").status_code == 400
    post(client, "start")
    with db.session() as conn:
        conn.execute("UPDATE plex_login SET expires=0")
    assert post(client, "poll").status_code == 400
    assert plex.settings()["key_set"]


def test_cookie_is_required(client, cloud):
    post(client, "start")
    other = client.application.test_client()
    assert post(other, "poll").status_code == 400
    assert post(other, "select", {"server": "0"}).status_code == 400


@pytest.mark.parametrize("selection", ["99", "-1", "http://attacker", None, 0])
def test_selection_must_be_offered(client, cloud, selection):
    state, _ = cloud
    post(client, "start")
    state["claimed"] = True
    post(client, "poll")
    assert post(client, "select", {"server": selection}).status_code == 400
    assert not plex.settings()["key_set"]


def test_unreachable_selection_preserves_settings(client, cloud, monkeypatch):
    state, _ = cloud
    monkeypatch.setenv(plex.KEYS["api_key"], "existing")
    post(client, "start")
    state["claimed"] = True
    post(client, "poll")

    def unavailable(payload):
        raise settings_store.SettingsError("Server unavailable")

    monkeypatch.setattr(plex, "libraries", unavailable)
    assert post(client, "select", {"server": "0"}).status_code == 400
    assert plex.settings()["key_set"]
    assert post(client, "poll").status_code == 200


@pytest.mark.parametrize("scope", ["read", "write"])
@pytest.mark.parametrize("action", ["start", "poll", "select", "cancel"])
def test_admin_required(client, api_key, scope, action):
    token, _ = api_key(scope=scope)
    assert (
        client.post(
            "/api/settings/plex/login/" + action, json={}, headers={"X-API-Key": token}
        ).status_code
        == 403
    )


def test_plain_form_rejected(client, cloud):
    assert (
        client.post(
            "/api/settings/plex/login/start", data={"action": "start"}
        ).status_code
        == 415
    )


def test_cloud_exception_sanitized(monkeypatch):
    import niquests

    def broken(*args, **kwargs):
        raise RuntimeError("account-secret private-url")

    monkeypatch.setattr(niquests.Session, "request", broken)
    with pytest.raises(settings_store.SettingsError) as error:
        plex_auth._cloud("POST", "pins", "client")
    assert "account-secret" not in str(error.value)
    assert "private-url" not in str(error.value)


def test_new_login_invalidates_previous(cloud):
    first, _ = plex_auth.start()
    second, _ = plex_auth.start(first)
    assert first != second
    with pytest.raises(settings_store.SettingsError):
        plex_auth.poll(first)
    assert plex_auth.poll(second) == {"status": "pending"}


def test_account_token_not_kept_after_server_discovery(client, cloud):
    state, _ = cloud
    post(client, "start")
    state["claimed"] = True
    post(client, "poll")
    with db.session() as conn:
        stored = conn.execute("SELECT data FROM plex_login").fetchone()[0]
    assert "account-secret" not in stored
    assert "server-secret" in stored
    assert "shared-secret" not in stored


def test_storage_failure_does_not_claim_success(client, cloud, monkeypatch):
    state, _ = cloud
    post(client, "start")
    state["claimed"] = True
    post(client, "poll")

    def fail(*args):
        raise OSError("private-server-secret")

    monkeypatch.setattr(settings_store, "_persist_settings", fail)
    result = post(client, "select", {"server": "0"})
    assert result.status_code == 500
    assert "private-server-secret" not in result.get_data(as_text=True)
    assert not plex.settings()["key_set"]
    assert post(client, "poll").status_code == 200


def test_local_address_uses_selected_server_token(client, cloud, monkeypatch):
    import os

    state, _ = cloud
    post(client, "start")
    state["claimed"] = True
    post(client, "poll")
    calls = []

    def libraries(payload):
        calls.append(payload)
        return []

    monkeypatch.setattr(plex, "libraries", libraries)
    response = post(
        client, "select", {"server": "0", "url": " http://192.168.178.59:32400/ "}
    )
    assert response.status_code == 200
    assert response.json["url"] == "http://192.168.178.59:32400"
    assert calls == [{"url": "http://192.168.178.59:32400", "api_key": "server-secret"}]
    assert os.environ[plex.KEYS["url"]] == response.json["url"]
    assert os.environ[plex.KEYS["api_key"]] == "server-secret"
    assert "server-secret" not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp",
        "http://user:pass@host",
        "http://host/?token=secret",
        "http://host/#secret",
        123,
    ],
)
def test_invalid_override_keeps_login_and_settings(client, cloud, monkeypatch, url):
    state, _ = cloud
    monkeypatch.setenv(plex.KEYS["url"], "http://old:32400")
    post(client, "start")
    state["claimed"] = True
    post(client, "poll")
    monkeypatch.setattr(
        plex, "libraries", lambda *args: pytest.fail("Invalid URL must not connect")
    )
    assert post(client, "select", {"server": "0", "url": url}).status_code == 400
    assert plex.settings()["url"] == "http://old:32400"
    assert post(client, "poll").status_code == 200


def test_failed_local_connection_can_be_corrected(client, cloud, monkeypatch):
    state, _ = cloud
    post(client, "start")
    state["claimed"] = True
    post(client, "poll")

    def unavailable(payload):
        raise settings_store.SettingsError("Connection failed")

    monkeypatch.setattr(plex, "libraries", unavailable)
    assert (
        post(client, "select", {"server": "0", "url": "http://wrong:32400"}).status_code
        == 400
    )
    assert not plex.settings()["key_set"]
    monkeypatch.setattr(plex, "libraries", lambda payload: [])
    assert (
        post(client, "select", {"server": "0", "url": "http://nas:32400"}).status_code
        == 200
    )


def test_empty_override_uses_discovered_connection(client, cloud):
    state, _ = cloud
    post(client, "start")
    state["claimed"] = True
    post(client, "poll")
    response = post(client, "select", {"server": "0", "url": " "})
    assert response.status_code == 200
    assert response.json["url"] == "http://nas:32400"
