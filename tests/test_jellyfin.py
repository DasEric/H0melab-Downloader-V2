from types import SimpleNamespace

import pytest

from h0melab.web import jellyfin, settings_store

MOVIE = "1" * 32
SERIES = "2" * 32


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        "url": "http://jellyfin:8096/base",
        "api_key": "secret",
        "movie_library_id": MOVIE,
        "series_library_id": SERIES,
    }.items():
        monkeypatch.setenv(jellyfin.KEYS[key], value)


@pytest.mark.parametrize(
    "kind,library,collection",
    [
        ("movie", MOVIE, "movies"),
        ("tv", SERIES, "tvshows"),
        ("series", SERIES, "tvshows"),
    ],
)
def test_targeted_refresh(configured, monkeypatch, kind, library, collection):
    calls = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kw):
            calls.append((url, kw))
            return SimpleNamespace(
                status_code=200,
                json=lambda: [{"ItemId": library, "CollectionType": collection}],
            )

        def post(self, url, **kw):
            calls.append((url, kw))
            return SimpleNamespace(status_code=204)

    monkeypatch.setattr(jellyfin.niquests, "Session", Session)
    assert jellyfin.refresh(kind)
    assert calls[1][0] == f"http://jellyfin:8096/base/Items/{library}/Refresh"
    assert calls[1][1]["params"]["ReplaceAllMetadata"] == "false"
    for url, kw in calls:
        assert kw["timeout"] == 10 and kw["allow_redirects"] is False
        assert kw["headers"] == {"X-Emby-Token": "secret"}
        assert "secret" not in url


@pytest.mark.parametrize("failure", ["wrong_type", "redirect", "timeout"])
def test_failure_does_not_escape_or_leak(configured, monkeypatch, caplog, failure):
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *a, **kw):
            if failure == "timeout":
                raise TimeoutError("secret private server")
            return SimpleNamespace(
                status_code=302 if failure == "redirect" else 200,
                json=lambda: [{"ItemId": MOVIE, "CollectionType": "tvshows"}],
            )

        def post(self, *a, **kw):
            pytest.fail("Must not refresh wrong library")

    monkeypatch.setattr(jellyfin.niquests, "Session", Session)
    assert not jellyfin.refresh("movie")
    assert "secret" not in caplog.text and "private server" not in caplog.text


def test_disabled_and_unknown_do_not_connect(monkeypatch):
    monkeypatch.setattr(jellyfin.niquests, "Session", lambda: pytest.fail("No request"))
    assert not jellyfin.refresh("movie")
    assert not jellyfin.refresh("manga")


@pytest.mark.parametrize(
    "payload",
    [
        {"url": "file:///tmp"},
        {"url": "https://user:pass@host"},
        {"url": "https://host/?token=x"},
        {"movie_library_id": "../all"},
        {"api_key": "bad\r\nheader"},
    ],
)
def test_invalid_settings(payload):
    with pytest.raises(settings_store.SettingsError):
        jellyfin.collect_settings(payload, {})


def test_settings_hide_key(configured):
    assert jellyfin.settings()["key_set"]
    assert "api_key" not in settings_store.read_settings()["jellyfin"]
    updates = {}
    jellyfin.collect_settings({"api_key": settings_store.SECRET_PLACEHOLDER}, updates)
    assert not updates


def test_settings_api_persists_without_secret_export(client, monkeypatch, tmp_path):
    from dotenv import dotenv_values

    path = tmp_path / "settings.env"
    monkeypatch.setattr(settings_store, "PANEL_SETTINGS_PATH", path)
    payload = {
        "url": "http://jellyfin:8096",
        "api_key": "jellyfin-private-key",
        "movie_library_id": MOVIE,
        "series_library_id": SERIES,
    }
    assert client.put("/api/settings", json={"jellyfin": payload}).status_code == 200
    assert dotenv_values(path)[jellyfin.KEYS["api_key"]] == payload["api_key"]
    assert payload["api_key"] not in client.get("/api/settings").get_data(as_text=True)
    assert payload["api_key"] not in settings_store.export_env()
    assert (
        client.put("/api/settings", json={"jellyfin": {"api_key": ""}}).status_code
        == 200
    )
    assert not jellyfin.settings()["key_set"]


def test_library_choices_use_draft_key_without_saving(client, monkeypatch):
    import os

    from h0melab.web.views import ADMIN_ENDPOINTS

    assert "api.jellyfin_libraries" in ADMIN_ENDPOINTS
    calls = []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(
                status_code=200,
                json=lambda: [
                    {"Name": "My movies", "ItemId": MOVIE, "CollectionType": "movies"},
                    {"Name": "My shows", "ItemId": SERIES, "CollectionType": "tvshows"},
                    {"Name": "Music", "ItemId": "3" * 32, "CollectionType": "music"},
                ],
            )

    monkeypatch.setattr(jellyfin.niquests, "Session", Session)
    response = client.post(
        "/api/settings/jellyfin/libraries",
        json={"url": "http://jellyfin:8096/base", "api_key": "draft-secret"},
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert len(response.get_json()["libraries"]) == 2
    assert calls[0][0] == "http://jellyfin:8096/base/Library/VirtualFolders"
    assert calls[0][1]["headers"]["X-Emby-Token"] == "draft-secret"
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["timeout"] == 10
    assert "draft-secret" not in response.get_data(as_text=True)
    assert not os.environ.get(jellyfin.KEYS["api_key"])


def test_library_choices_saved_key_and_generic_failure(client, configured, monkeypatch):
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            assert kwargs["headers"]["X-Emby-Token"] == "secret"
            raise TimeoutError("private-secret-data")

    monkeypatch.setattr(jellyfin.niquests, "Session", Session)
    response = client.post("/api/settings/jellyfin/libraries", json={})
    assert response.status_code == 400
    assert "private-secret-data" not in response.get_data(as_text=True)


@pytest.mark.parametrize("scope", ["read", "write"])
def test_library_choices_require_admin(client, api_key, monkeypatch, scope):
    monkeypatch.setattr(
        jellyfin, "libraries", lambda *args: pytest.fail("Must not contact Jellyfin")
    )
    raw, _ = api_key(scope=scope)
    response = client.post(
        "/api/settings/jellyfin/libraries", json={}, headers={"X-API-Key": raw}
    )
    assert response.status_code == 403
