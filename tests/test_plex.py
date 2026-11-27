from types import SimpleNamespace

import pytest

from h0melab.web import plex, settings_store


@pytest.fixture
def server(monkeypatch):
    calls = []
    for name, value in {
        "url": "http://plex:32400",
        "api_key": "private-token",
        "movie_library_id": "1",
        "series_library_id": "2",
    }.items():
        monkeypatch.setenv(plex.KEYS[name], value)

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kw):
            calls.append((url, kw))
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "MediaContainer": {
                        "Directory": [
                            {"key": "1", "type": "movie", "title": "Movies"},
                            {"key": "2", "type": "show", "title": "Shows"},
                            {"key": "3", "type": "artist", "title": "Music"},
                        ]
                    }
                },
            )

    monkeypatch.setattr(plex.niquests, "Session", Session)
    return calls


@pytest.mark.parametrize("kind,section", [("movie", "1"), ("tv", "2"), ("series", "2")])
def test_typed_refresh(server, kind, section):
    assert plex.refresh(kind)
    assert server[-1][0] == "http://plex:32400/library/sections/" + section + "/refresh"
    for url, kw in server:
        assert "private-token" not in url
        assert kw["headers"]["X-Plex-Token"] == "private-token"
        assert kw["allow_redirects"] is False
        assert kw["timeout"] == 10


def test_choices_and_secret(server, client, monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings_store, "PANEL_SETTINGS_PATH", tmp_path / "settings.env"
    )
    response = client.post("/api/settings/plex/libraries", json={})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert [row["type"] for row in response.json["libraries"]] == [
        "movies",
        "tvshows",
        "music",
    ]
    assert "private-token" not in client.get("/api/settings").get_data(as_text=True)
    assert "private-token" not in settings_store.export_env()
    assert (
        client.put(
            "/api/settings", json={"plex": {"api_key": "replacement"}}
        ).status_code
        == 200
    )
    assert plex.settings()["key_set"]
    assert "replacement" not in settings_store.export_env()
    assert (
        client.put("/api/settings", json={"plex": {"api_key": ""}}).status_code == 200
    )
    assert not plex.settings()["key_set"]


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "file:///tmp"},
        {"url": "http://user:password@host"},
        {"movie_library_id": "../all"},
        {"api_key": "bad\r\nheader"},
    ],
)
def test_invalid(payload):
    with pytest.raises(settings_store.SettingsError):
        plex.collect_settings(payload, {})


def test_wrong_type_no_scan(server, monkeypatch):
    monkeypatch.setenv(plex.KEYS["movie_library_id"], "2")
    assert not plex.refresh("movie")
    assert len(server) == 1


def test_disabled_does_not_connect(monkeypatch):
    for key in plex.KEYS.values():
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(plex.niquests, "Session", lambda: pytest.fail("No request"))
    assert not plex.refresh("movie")
    assert not plex.refresh("manga")


@pytest.mark.parametrize("code", [401, 403, 302, 404, 500])
def test_http_errors(code):
    session = SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(status_code=code))
    with pytest.raises(settings_store.SettingsError, match=str(code)):
        plex._request(session, "http://plex/library/sections", "private-token")


@pytest.mark.parametrize("scope", ["read", "write"])
def test_admin_only(client, api_key, monkeypatch, scope):
    monkeypatch.setattr(plex, "libraries", lambda *a: pytest.fail("No request"))
    raw, _ = api_key(scope=scope)
    assert (
        client.post(
            "/api/settings/plex/libraries", json={}, headers={"X-API-Key": raw}
        ).status_code
        == 403
    )


def test_draft_token_does_not_persist(server):
    import os

    assert (
        len(plex.libraries({"api_key": "draft-token"})) == 3
    )  # movies, shows and music (Plex "artist")
    assert server[0][1]["headers"]["X-Plex-Token"] == "draft-token"
    assert os.environ[plex.KEYS["api_key"]] == "private-token"


def test_scan_failure_does_not_escape_or_leak(server, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError("private-token private-url")

    monkeypatch.setattr(plex, "libraries", broken)
    assert not plex.refresh("movie")
    assert "private-token" not in caplog.text
    assert "private-url" not in caplog.text


def test_settings_card(client):
    response = client.get("/settings")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'id="plexSettings"' in page
    assert 'id="jellyfinSettings"' in page
    assert "plex-settings.js" in page
