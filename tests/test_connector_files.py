"""Exact-file ownership receipts, routing, worker and full-access API contracts."""

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from h0melab.web import connector_files as files
from h0melab.web import db, worker


@pytest.fixture(autouse=True)
def no_probe_by_default(monkeypatch):
    monkeypatch.setattr(
        files,
        "media_streams",
        lambda path: [{"codec_type": "audio", "tags": {"language": "deu"}}],
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


def test_paths_outside_root_and_hardlinks_are_rejected(downloads, tmp_path):
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"other")
    with pytest.raises(ValueError):
        files.checked_path(downloads, outside)
    linked = downloads / "linked.mkv"
    linked.hardlink_to(outside)
    with pytest.raises(ValueError):
        files.checked_path(downloads, linked)


def test_variant_filename_is_separate_and_frozen(downloads):
    episode = SimpleNamespace(_connector_variant="German Dub")
    first = files.episode_path(episode, downloads / "Title S01E001.mkv")
    assert first.name == "Title S01E001 - german-dub.mkv"
    assert files.episode_path(episode, downloads / "Changed.mkv") == first


@pytest.mark.parametrize("collision", [False, True])
def test_resolution_finalization_never_clobbers_another_file(
    downloads, monkeypatch, collision
):
    from h0melab.models.common import common

    monkeypatch.setattr(common, "_naming_template_uses_resolution", lambda: True)
    monkeypatch.setattr(common, "_read_container_resolution", lambda path: "1080p")

    class Episode:
        _connector_variant = "German Dub"
        _connector_root = downloads
        _resolution = "unknown"

        @property
        def _episode_path(self):
            return files.episode_path(
                self, downloads / f"Show S01E001 {self._resolution}.mkv"
            )

    episode = Episode()
    original = episode._episode_path
    original.write_bytes(b"my new file")
    target = downloads / "Show S01E001 1080p - german-dub.mkv"
    if collision:
        target.write_bytes(b"foreign")
    common._finalize_resolution_naming(episode)
    if collision:
        assert target.read_bytes() == b"foreign" and episode._episode_path == original
    else:
        assert target.read_bytes() == b"my new file" and not original.exists()
        assert episode._episode_path == target


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


def test_api_rejects_missing_and_changed_targets(client, downloads):
    entry = {
        "url": "https://example.test/1",
        "language": "German Dub",
        "provider": "VOE",
        "media_type": "series",
        "target_root": str(downloads),
        "custom_path_id": 9999,
    }
    assert (
        client.post(
            "/api/download",
            json={
                "connector_protocol": 1,
                "request_key": "a" * 32,
                "episodes": [entry],
            },
        ).status_code
        == 409
    )


def test_download_key_cannot_create_connector_receipts(client, api_key, downloads):
    key, _ = api_key("write")
    entry = {
        "url": "https://example.test/1",
        "language": "German Dub",
        "provider": "VOE",
        "media_type": "series",
        "target_root": str(downloads),
    }
    assert (
        client.post(
            "/api/download",
            headers={"X-API-Key": key},
            json={
                "connector_protocol": 1,
                "request_key": "a" * 32,
                "episodes": [entry],
            },
        ).status_code
        == 403
    )


def test_internal_marker_cannot_be_smuggled_into_legacy_queue(client):
    assert (
        client.post(
            "/api/download",
            json={
                "request_key": "a" * 32,
                "episodes": [
                    {"url": "https://example.test/1", "connector_protocol": 1}
                ],
            },
        ).status_code
        == 400
    )


def test_worker_routes_each_entry_and_records_new_files(
    client, downloads, tmp_path, monkeypatch
):
    movies = tmp_path / "movies"
    movies.mkdir()
    path_id = db.add_custom_path("Movies", str(movies))
    entries = [
        {
            "url": "https://example.test/series",
            "language": "German Dub",
            "provider": "VOE",
            "media_type": "series",
            "target_root": str(downloads),
        },
        {
            "url": "https://example.test/film",
            "language": "German Dub",
            "provider": "Filemoon",
            "media_type": "movie",
            "target_root": str(movies),
            "custom_path_id": path_id,
        },
    ]
    calls = []

    def build(url, extra, options, selected_path):
        calls.append((options["media_type"], options["provider"], selected_path))

        class Episode:
            @property
            def _episode_path(self):
                return files.episode_path(
                    self,
                    Path(selected_path)
                    / (
                        "Series S01E001.mkv"
                        if options["media_type"] == "series"
                        else "Film.mkv"
                    ),
                )

            def download(self):
                self._episode_path.write_bytes(b"finished")

        return SimpleNamespace(name="Test"), Episode()

    monkeypatch.setattr(worker, "_build_episode", build)
    response = client.post(
        "/api/download",
        json={
            "connector_protocol": 1,
            "request_key": "a" * 32,
            "episodes": entries,
            "language": "German Dub",
            "provider": "Automatic",
        },
    )
    assert response.status_code == 200
    identity = response.get_json()["queue_id"]
    worker._process(db.get_queue_item(identity))
    assert db.get_queue_item(identity)["status"] == "completed"
    assert calls == [
        ("series", "VOE", str(downloads)),
        ("movie", "Filemoon", str(movies)),
    ]
    assert {item["media_type"] for item in files.query(["a" * 32])} == {
        "series",
        "movie",
    }
    assert all(item["state"] == "owned" for item in files.query(["a" * 32]))


def test_real_ffprobe_reports_german_and_english_audio(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is not installed")
    path = tmp_path / "dual.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=16x16:duration=0.1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc",
            "-f",
            "lavfi",
            "-i",
            "anullsrc",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
            "-shortest",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-metadata:s:a:0",
            "language=deu",
            "-metadata:s:a:1",
            "language=eng",
            str(path),
        ],
        check=True,
        timeout=30,
    )
    # Reimport the actual implementation after the fixture's default fake.
    import importlib

    real = importlib.reload(files).media_streams(path)
    assert files.audio_matches(real, "German Dub") is True
    assert files.audio_matches(real, "English Dub") is True
    assert files.audio_matches([], "German Dub") is None
