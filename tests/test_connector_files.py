"""Jellyfin connector: exact-file receipts, typed routing, worker and API access."""

import json
from pathlib import Path

import pytest

from h0melab.web import connector_files as files
from h0melab.web import db, media_files, media_targets, video_library, worker

SHOW_URL = "https://megakino.example/serien/gravity-falls/s01e01"
FILM_URL = "https://megakino.example/filme/a-film"
IDENTITIES = {
    SHOW_URL: {
        "media_type": "series",
        "title": "Gravity Falls",
        "year": "2012",
        "season": 1,
        "episode": 1,
        "end": 1,
    },
    FILM_URL: {"media_type": "movie", "title": "A Film", "year": "2020"},
}


@pytest.fixture(autouse=True)
def german_audio(monkeypatch):
    monkeypatch.setattr(
        files,
        "media_streams",
        lambda path: [{"codec_type": "audio", "tags": {"language": "deu"}}],
    )


@pytest.fixture
def typed(downloads, tmp_path):
    """Movies and shows as separate central targets; MegaKino defaults to movies."""
    movies = tmp_path / "movies"
    movies.mkdir()
    movie_id = db.add_custom_path("Movies", str(movies), "megakino")
    show_id = db.add_custom_path("Shows", str(downloads))
    media_targets.save(movie_id, show_id, 0)
    return movie_id, show_id, movies, downloads


@pytest.fixture
def megakino(monkeypatch):
    """A source whose site default path is the movie folder, serving both types."""
    from h0melab.web import jellyfin, plex

    monkeypatch.setattr(
        video_library, "inspect", lambda url, language=None: dict(IDENTITIES[url])
    )
    monkeypatch.setattr(jellyfin, "refresh", lambda kind: None)
    monkeypatch.setattr(plex, "refresh", lambda kind: None)
    calls = []

    def build(url, extra, options, selected_path):
        calls.append(
            (url, options["media_type"], options["provider"], options["language"])
        )

        class Episode:
            def __init__(self):
                self.video_identity = dict(IDENTITIES[url])
                self.selected_path = selected_path

            @property
            def _episode_path(self):
                return media_files.episode_path(
                    self, Path(self.selected_path) / "video.mkv"
                )

            def download(self):
                self._episode_path.write_bytes(b"finished")

        return type("Provider", (), {"name": "MegaKino"})(), Episode()

    monkeypatch.setattr(worker, "_build_episode", build)
    return calls


def entry(url, typed, media_type, language="German Dub", provider="VOE"):
    movie = media_type == "movie"
    return {
        "url": url,
        "language": language,
        "provider": provider,
        "media_type": media_type,
        "custom_path_id": typed[0 if movie else 1],
        "target_root": str(Path(typed[2 if movie else 3]).absolute()),
    }


def submit(client, episodes, key="a" * 32, **extra):
    return client.post(
        "/api/download",
        json={
            "connector_protocol": 1,
            "request_key": key,
            "title": "Gravity Falls",
            "episodes": episodes,
            "language": "German Dub",
            "provider": "Automatic",
            **extra,
        },
    )


def receipt(path, root, key="a" * 32, existed=False, index=0, path_id=None):
    files.record(
        {"request_key": key, "id": 1},
        index,
        path,
        root,
        existed,
        {"custom_path_id": path_id, "media_type": "series", "language": "German Dub"},
    )
    return files.query([key])[-1]


def test_instance_identity_is_durable():
    assert files.capabilities()["instance_id"] == files.capabilities()["instance_id"]


def test_owned_file_survives_queue_cleanup_and_deletes_exactly_one(
    episode_file, downloads
):
    own = episode_file("Shared", 1, 1)
    foreign = episode_file("Shared", 1, 2)
    artifact = receipt(own, downloads)
    db.clear_completed()
    assert files.delete([artifact["id"]]) == [
        {"id": artifact["id"], "status": "deleted"}
    ]
    assert foreign.exists() and not own.exists()
    assert files.query(["a" * 32])[0]["state"] == "deleted"


def test_existing_file_does_not_gain_ownership(episode_file, downloads):
    path = episode_file("Shared", 1, 1)
    item = receipt(path, downloads, existed=True)
    assert item["state"] == "existing"
    assert files.delete([item["id"]])[0]["status"] == "conflict"
    assert path.exists()


def test_replacement_is_not_deleted(episode_file, downloads):
    path = episode_file("Shared", 1, 1)
    item = receipt(path, downloads)
    path.write_bytes(b"replaced")
    assert files.query(["a" * 32])[0]["state"] == "changed"
    assert files.delete([item["id"]])[0]["status"] == "conflict"
    assert path.read_bytes() == b"replaced"


def test_missing_file_and_repeat_confirmation_are_idempotent(episode_file, downloads):
    path = episode_file("Shared", 1, 1)
    item = receipt(path, downloads)
    path.unlink()
    assert files.delete([item["id"]])[0]["status"] == "deleted"
    assert files.delete([item["id"]])[0]["status"] == "deleted"


def test_modified_mount_is_not_followed(episode_file, downloads, tmp_path):
    path_id = db.add_custom_path("Original", str(downloads))
    path = episode_file("Shared", 1, 1)
    item = receipt(path, downloads, path_id=path_id)
    other = tmp_path / "other"
    other.mkdir()
    db.update_custom_path(path_id, path=str(other))
    assert files.query(["a" * 32])[0]["state"] == "changed"
    assert files.delete([item["id"]])[0]["status"] == "conflict"
    assert path.exists()


def test_busy_file_and_other_files_are_independent(episode_file, downloads):
    path = episode_file("Shared", 1, 1)
    item = receipt(path, downloads)
    with files.file_operation(path):
        assert files.delete([item["id"]])[0]["status"] == "conflict"
    assert files.delete([item["id"]])[0]["status"] == "deleted"


def test_partial_delete_reports_each_file(episode_file, downloads):
    first, second = episode_file("Shared", 1, 1), episode_file("Shared", 1, 2)
    a = receipt(first, downloads, index=0)
    b = receipt(second, downloads, index=1)
    second.write_bytes(b"changed")
    result = files.delete([a["id"], b["id"]])
    assert [row["status"] for row in result] == ["deleted", "conflict"]
    assert second.exists()


def test_receipt_retries_keep_the_same_owned_file(episode_file, downloads):
    path = episode_file("Shared", 1, 1)
    first = receipt(path, downloads)
    second = receipt(path, downloads, existed=True)
    assert first["id"] == second["id"] and second["state"] == "owned"


@pytest.mark.parametrize("scope", ["read", "write"])
def test_receipts_and_deletion_need_full_access(client, api_key, scope):
    key, _ = api_key(scope)
    headers = {"X-API-Key": key}
    assert client.get("/api/connector/capabilities", headers=headers).status_code == 403
    assert (
        client.post(
            "/api/connector/artifacts/query",
            headers=headers,
            json={"request_keys": ["a" * 32]},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/connector/artifacts/delete", headers=headers, json={"ids": ["a" * 32]}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/connector/path-probe", headers=headers, json={"path_id": 1}
        ).status_code
        == 403
    )


def test_capabilities_match_the_connector_contract(client, typed):
    data = client.get("/api/connector/capabilities").get_json()
    assert data["protocol"] == 1 and data["file_receipts"] and data["entry_options"]
    assert data["video_groups"] >= 1 and data["max_entries"] >= 500
    assert len(data["instance_id"]) == 32
    assert data["media_targets"]["enabled"] is True
    assert {path["id"] for path in data["paths"]} >= {typed[0], typed[1]}


def test_path_probe_creates_a_real_shared_file_and_cleans_up(client, downloads):
    path_id = db.add_custom_path("Movies", str(downloads))
    response = client.post("/api/connector/path-probe", json={"path_id": path_id})
    assert response.status_code == 200
    data = response.get_json()
    assert (downloads / data["relative_path"]).read_text() == data["nonce"]
    assert (
        client.post(
            "/api/connector/path-probe",
            json={"path_id": path_id, "probe_id": data["probe_id"], "cleanup": True},
        ).status_code
        == 200
    )
    assert not (downloads / data["relative_path"]).exists()


def test_connector_needs_central_destinations_and_never_uses_a_site_default(
    client, downloads, tmp_path, megakino
):
    movies = tmp_path / "movies"
    movies.mkdir()
    movie_id = db.add_custom_path("Movies", str(movies), "megakino")
    show = {
        "url": SHOW_URL,
        "language": "German Dub",
        "provider": "VOE",
        "media_type": "series",
        "custom_path_id": movie_id,
        "target_root": str(movies.absolute()),
    }
    response = submit(client, [show])
    assert response.status_code == 409
    assert "destinations" in response.get_json()["error"]
    assert db.get_queue() == []


def test_api_rejects_missing_and_changed_targets(client, typed, megakino):
    show = entry(SHOW_URL, typed, "series")
    assert submit(client, [{**show, "custom_path_id": 9999}]).status_code == 409
    assert submit(client, [{**show, "target_root": "/elsewhere"}]).status_code == 409
    assert db.get_queue() == []


def test_show_entry_aimed_at_the_movie_target_is_rejected(client, typed, megakino):
    show = entry(SHOW_URL, typed, "series")
    show.update(custom_path_id=typed[0], target_root=str(Path(typed[2]).absolute()))
    response = submit(client, [show])
    assert response.status_code == 409
    assert db.get_queue() == []


def test_download_key_cannot_create_connector_receipts(client, api_key, typed):
    key, _ = api_key("write")
    response = client.post(
        "/api/download",
        headers={"X-API-Key": key},
        json={
            "connector_protocol": 1,
            "request_key": "a" * 32,
            "episodes": [entry(SHOW_URL, typed, "series")],
        },
    )
    assert response.status_code == 403


def test_internal_marker_cannot_be_smuggled_into_legacy_queue(client):
    response = client.post(
        "/api/download",
        json={
            "request_key": "a" * 32,
            "episodes": [{"url": "https://example.test/1", "connector_protocol": 1}],
        },
    )
    assert response.status_code == 400


def test_megakino_show_and_film_land_in_their_own_folders_with_receipts(
    client, typed, megakino
):
    _movie_id, _show_id, movies, shows = typed
    response = submit(
        client,
        [
            entry(SHOW_URL, typed, "series", provider="VOE"),
            entry(FILM_URL, typed, "movie", provider="Filemoon"),
        ],
    )
    assert response.status_code == 200, response.get_json()
    item = db.get_queue_item(response.get_json()["queue_id"])
    assert item["payload_version"] == 3
    worker._process(item)
    assert db.get_queue_item(item["id"])["status"] == "completed"
    # Each entry keeps its own verified variant and provider.
    assert [call[1:] for call in megakino] == [
        ("series", "VOE", "German Dub"),
        ("movie", "Filemoon", "German Dub"),
    ]
    show_files = list(shows.rglob("*.mkv"))
    film_files = list(movies.rglob("*.mkv"))
    assert [path.relative_to(shows).as_posix() for path in show_files] == [
        "Gravity Falls (2012)/Season 01/Gravity Falls (2012) - S01E01 - german-dub.mkv"
    ]
    assert [path.relative_to(movies).as_posix() for path in film_files] == [
        "A Film (2020)/A Film (2020) - german-dub.mkv"
    ]
    receipts = files.query(["a" * 32])
    assert sorted((row["media_type"], row["state"]) for row in receipts) == [
        ("movie", "owned"),
        ("series", "owned"),
    ]
    # Receipts stay deletable exactly as recorded.
    result = files.delete([row["id"] for row in receipts])
    assert {row["status"] for row in result} == {"deleted"}
    assert not list(shows.rglob("*.mkv")) and not list(movies.rglob("*.mkv"))


def test_existing_file_is_not_downloaded_again_and_not_owned(client, typed, megakino):
    target = (
        typed[3]
        / "Gravity Falls (2012)/Season 01/Gravity Falls (2012) - S01E01 - german-dub.mkv"
    )
    target.parent.mkdir(parents=True)
    target.write_bytes(b"manual copy")
    response = submit(client, [entry(SHOW_URL, typed, "series")])
    worker._process(db.get_queue_item(response.get_json()["queue_id"]))
    assert target.read_bytes() == b"manual copy"
    assert [row["state"] for row in files.query(["a" * 32])] == ["existing"]


def test_wrong_audio_fails_the_entry_but_keeps_its_receipt(
    client, typed, megakino, monkeypatch
):
    monkeypatch.setattr(
        files,
        "media_streams",
        lambda path: [{"codec_type": "audio", "tags": {"language": "eng"}}],
    )
    response = submit(client, [entry(SHOW_URL, typed, "series")])
    item_id = response.get_json()["queue_id"]
    worker._process(db.get_queue_item(item_id))
    item = db.get_queue_item(item_id)
    assert item["status"] == "failed"
    assert "audio" in json.loads(item["errors"])[0]["error"]
    # The user can still remove the file the request produced.
    assert [row["state"] for row in files.query(["a" * 32])] == ["owned"]


def test_removed_destinations_stop_a_queued_connector_job(client, typed, megakino):
    response = submit(client, [entry(SHOW_URL, typed, "series")])
    with db.session() as conn:
        conn.execute("DELETE FROM media_targets")
    item_id = response.get_json()["queue_id"]
    worker._process(db.get_queue_item(item_id))
    assert db.get_queue_item(item_id)["status"] == "failed"
    assert not list(typed[2].rglob("*.mkv")) and not list(typed[3].rglob("*.mkv"))
