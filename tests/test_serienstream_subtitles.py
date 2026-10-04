import json
import shutil
import subprocess

import pytest

from h0melab.extractors.provider import voe
from h0melab.models.common import common
from h0melab.models.common.subtitles import (
    MediaAsset,
    SubtitleTrack,
    normalize_subtitle_language,
)
from h0melab.models.s_to.episode import SerienstreamEpisode
from h0melab.web import db, worker


@pytest.mark.parametrize(
    "value,expected",
    [
        ("de", "deu"),
        ("de-DE", "deu"),
        ("ger", "deu"),
        ("Deutsch", "deu"),
        ("none", "none"),
    ],
)
def test_subtitle_language_normalization(value, expected):
    assert normalize_subtitle_language(value) == expected


def test_voe_player_captions_are_kept(monkeypatch):
    monkeypatch.setattr(
        voe,
        "_decode_voe_payloads",
        lambda _html: [
            {
                "source": "https://cdn.example/video.m3u8",
                "default_captions_language": "de",
                "captions": [{"file": "/captions/episode.vtt", "label": "Deutsch"}],
            }
        ],
    )

    asset = voe.extract_voe_media_asset_from_html(
        "ignored", "https://player.example/e/1", {"Referer": "https://player.example"}
    )

    assert asset.video_url == "https://cdn.example/video.m3u8"
    assert asset.subtitle("deu").url == "https://player.example/captions/episode.vtt"
    assert asset.subtitle("deu").headers["Referer"] == "https://player.example/e/1"


def test_voe_collects_jwplayer_caption_from_a_separate_payload(monkeypatch):
    monkeypatch.setattr(
        voe,
        "_decode_voe_payloads",
        lambda _html: [
            {"sources": [{"file": "https://cdn.example/video.m3u8"}]},
            {
                "tracks": [
                    {
                        "kind": "captions",
                        "file": "https://cdn.example/de.vtt",
                        "label": "Deutsch",
                    },
                    {
                        "kind": "thumbnails",
                        "file": "https://cdn.example/thumbs.vtt",
                    },
                ]
            },
        ],
    )

    asset = voe.extract_voe_media_asset_from_html(
        "ignored", "https://player.example/e/1"
    )

    assert asset.video_url == "https://cdn.example/video.m3u8"
    assert [track.url for track in asset.subtitles] == ["https://cdn.example/de.vtt"]


def test_serienstream_requires_requested_subtitle_from_same_hoster(monkeypatch):
    episode = SerienstreamEpisode(
        "https://serienstream.to/serie/dark/staffel-1/episode-1",
        selected_provider="VOE",
        selected_subtitle_language="deu",
    )
    monkeypatch.setattr(
        SerienstreamEpisode,
        "provider_url",
        property(lambda _self: "https://voe.sx/e/x"),
    )
    monkeypatch.setitem(
        __import__(
            "h0melab.models.s_to.episode", fromlist=["provider_functions"]
        ).provider_functions,
        "get_media_asset_from_voe",
        lambda _url: MediaAsset("https://cdn.example/video.m3u8", ()),
    )

    with pytest.raises(ValueError, match="no German subtitles"):
        _ = episode.stream_url


def test_download_api_persists_subtitle_choice(client):
    response = client.post(
        "/api/download",
        json={
            "title": "Dark",
            "series_url": "https://serienstream.to/serie/dark",
            "episodes": ["https://serienstream.to/serie/dark/staffel-1/episode-1"],
            "language": "German Dub",
            "provider": "VOE",
            "subtitle_language": "deu",
        },
    )
    assert response.status_code == 200
    assert (
        db.get_queue_item(response.get_json()["queue_id"])["subtitle_language"] == "deu"
    )


def test_download_api_rejects_subtitles_for_other_catalogues(client):
    response = client.post(
        "/api/download",
        json={
            "episodes": ["https://aniworld.to/anime/stream/x/staffel-1/episode-1"],
            "subtitle_language": "deu",
        },
    )
    assert response.status_code == 400


def test_worker_forwards_autosync_subtitle_setting(monkeypatch, tmp_path):
    class FakeEpisode:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeProvider:
        name = "SerienStream"
        episode_cls = FakeEpisode

    monkeypatch.setattr(worker, "resolve_provider", lambda _url: FakeProvider())
    _provider, episode = worker._build_episode(
        "https://serienstream.to/serie/dark/staffel-1/episode-1",
        {},
        {
            "language": "German Dub",
            "provider": "VOE",
            "subtitle_language": "deu",
        },
        tmp_path,
    )

    assert episode.kwargs["selected_subtitle_language"] == "deu"


def test_existing_german_subtitle_alias_is_recognized():
    assert common._has_requested_subtitle({"subtitle_langs": {"ger"}}, "deu")


def test_provider_retry_clears_cached_video_and_subtitle_asset():
    class Owner:
        pass

    owner = Owner()
    owner._SerienstreamEpisode__redirect_url = "redirect"
    owner._SerienstreamEpisode__provider_url = "provider"
    owner._SerienstreamEpisode__media_asset = object()

    common._reset_provider_resolution_cache(owner)

    assert owner._SerienstreamEpisode__redirect_url is None
    assert owner._SerienstreamEpisode__provider_url is None
    assert owner._SerienstreamEpisode__media_asset is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is not installed")
def test_mkv_remux_keeps_soft_subtitle_timing(tmp_path, monkeypatch):
    video = tmp_path / "episode.mkv"
    subtitle_source = tmp_path / "source.vtt"
    subtitle_source.write_text(
        "WEBVTT\n\n00:00:00.500 --> 00:00:01.500\nHallo Welt\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x64:duration=2",
            "-c:v",
            "mpeg4",
            str(video),
        ],
        check=True,
    )

    track = SubtitleTrack("https://example.invalid/episode.vtt", "deu", "Deutsch")

    class Owner:
        selected_subtitle_language = "deu"

        @staticmethod
        def selected_subtitle_track():
            return track

    def copy_subtitle(_track, output):
        shutil.copyfile(subtitle_source, output)
        return output

    import h0melab.models.common.subtitles as subtitle_module

    monkeypatch.setattr(subtitle_module, "download_subtitle", copy_subtitle)
    common._embed_requested_subtitle(Owner(), video, "episode", progress_start=0)

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "s:0",
            "-show_entries",
            "stream=codec_name:stream_tags=language,title",
            "-of",
            "json",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["tags"] == {"language": "deu", "title": "Deutsch"}

    packets = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "s:0",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "json",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert float(json.loads(packets.stdout)["packets"][0]["pts_time"]) == pytest.approx(
        0.5
    )


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is not installed")
def test_mp4_remux_keeps_selectable_subtitle_and_timing(tmp_path, monkeypatch):
    video = tmp_path / "episode.mp4"
    subtitle_source = tmp_path / "source.vtt"
    extracted = tmp_path / "extracted.srt"
    subtitle_source.write_text(
        "WEBVTT\n\n00:00:00.750 --> 00:00:01.250\nHallo Welt\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x64:duration=2",
            "-c:v",
            "libx264",
            str(video),
        ],
        check=True,
    )

    track = SubtitleTrack("https://example.invalid/episode.vtt", "deu", "Deutsch")

    class Owner:
        selected_subtitle_language = "deu"

        @staticmethod
        def selected_subtitle_track():
            return track

    def copy_subtitle(_track, output):
        shutil.copyfile(subtitle_source, output)
        return output

    import h0melab.models.common.subtitles as subtitle_module

    monkeypatch.setattr(subtitle_module, "download_subtitle", copy_subtitle)
    common._embed_requested_subtitle(Owner(), video, "episode", progress_start=0)

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "s:0",
            "-show_entries",
            "stream=codec_name:stream_tags=language,handler_name",
            "-of",
            "json",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["codec_name"] == "mov_text"
    assert stream["tags"]["language"] == "deu"
    assert stream["tags"]["handler_name"] == "Deutsch"

    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-map",
            "0:s:0",
            str(extracted),
        ],
        check=True,
    )
    assert "00:00:00,750 --> 00:00:01,250" in extracted.read_text(encoding="utf-8-sig")
