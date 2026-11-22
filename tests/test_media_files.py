"""Shared filesystem protection and video routing regressions."""

import shutil
import subprocess
from types import SimpleNamespace

import pytest

from h0melab.web import media_files as files


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
    episode = SimpleNamespace(_media_variant="German Dub")
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
        _media_variant = "German Dub"
        _media_root = downloads
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
