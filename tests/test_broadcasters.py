"""Native broadcaster contracts, with no live services or yt-dlp calls."""

from pathlib import Path

import pytest

from h0melab.models.broadcasters import DMAXEpisode, ZDFEpisode
from h0melab.models.broadcasters.client import BroadcasterError, MediaFormat, Video
from h0melab.models.broadcasters.dmax import DMAXClient
from h0melab.models.broadcasters.zdf import ZDFClient
from h0melab.providers import resolve_provider
from h0melab.web import media, worker

ZDF_URL = "https://www.zdf.de/video/dokus/test-100/episode-100"
DMAX_URL = "https://dmax.de/sendungen/test-show/test-episode"


@pytest.fixture
def video():
    return Video(
        "123",
        "Test episode",
        series="Test show",
        season=2,
        episode=3,
        formats=[MediaFormat("https://cdn.example/video.mp4", "http")],
    )


@pytest.mark.parametrize("cls,url", [(ZDFEpisode, ZDF_URL), (DMAXEpisode, DMAX_URL)])
def test_provider_and_worker_contract(cls, url, video, monkeypatch):
    monkeypatch.setattr(cls, "_fetch", lambda self: video)
    assert resolve_provider(url).episode_cls is cls
    provider, episode = worker._build_episode(
        url, {}, {"language": "German Dub", "provider": "Automatic"}, None
    )
    assert provider.name == cls.name
    assert media.provider_map(episode.provider_data) == {"German Dub": [cls.name]}
    assert episode.video_identity["title"] == "Test show"
    assert episode.video_identity["season"] == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://zdf.de.evil.test/video/a",
        "https://dmax.de.evil.test/sendungen/a/b",
    ],
)
def test_reject_non_episode_or_fake_host(url):
    with pytest.raises(ValueError):
        resolve_provider(url)


def test_zdf_extracts_tracks_and_captions(monkeypatch):
    def request(self, url, **kwargs):
        if url.endswith("/token"):
            return {"type": "Bearer", "token": "test", "expires": 9999999999}
        if url.endswith("/graphql"):
            return {
                "data": {
                    "videoByCanonical": {
                        "title": "Test",
                        "episodeInfo": {},
                        "currentMedia": {
                            "nodes": [{"ptmdTemplate": "/tmd/{playerId}/test"}]
                        },
                    }
                }
            }
        return {
            "captions": [{"uri": "https://cdn.example/de.vtt", "language": "de"}],
            "priorityList": [
                {
                    "formitaeten": [
                        {
                            "qualities": [
                                {
                                    "audio": {
                                        "tracks": [
                                            {
                                                "uri": "https://cdn.example/video.mp4",
                                                "language": "de",
                                                "class": "main",
                                            },
                                            {
                                                "uri": "https://cdn.example/video.mp4",
                                                "language": "de",
                                                "class": "main",
                                            },
                                            {
                                                "uri": "https://cdn.example/ad.mp4",
                                                "language": "de",
                                                "class": "ad",
                                            },
                                        ]
                                    }
                                }
                            ]
                        }
                    ]
                }
            ],
        }

    monkeypatch.setattr(ZDFClient, "request", request)
    client = ZDFClient()
    try:
        result = client.extract(ZDF_URL)
        assert len(result.formats) == 2
        assert result.formats[0].language == "deu"
        assert result.subtitles[0].language == "deu"
    finally:
        client.close()


def test_dmax_aurora_metadata_and_playback(monkeypatch):
    calls = []

    def request(self, url, **kwargs):
        calls.append((url, kwargs))
        if "site/shows/" in url:
            return {"title": "Show", "attributes": {"showId": "42"}}
        if url.endswith("token"):
            return {"data": {"attributes": {"token": "test"}}}
        if url.endswith("content/videos"):
            return {
                "data": [{"id": "123", "attributes": {"alternateId": "test-episode"}}],
                "meta": {"totalPages": 1},
            }
        if "content/videos" in url:
            return {
                "data": {
                    "id": "123",
                    "attributes": {
                        "name": "Episode",
                        "seasonNumber": 1,
                        "episodeNumber": 2,
                    },
                },
                "included": [{"type": "show", "attributes": {"name": "Show"}}],
            }
        return {
            "data": {
                "attributes": {
                    "streaming": [
                        {"type": "hls", "url": "https://cdn.example/master.m3u8"}
                    ]
                }
            }
        }

    monkeypatch.setattr(DMAXClient, "request", request)
    client = DMAXClient()
    try:
        result = client.extract(DMAX_URL)
        assert result.series == "Show"
        assert result.episode == 2
        assert result.formats[0].protocol == "hls"
        assert calls[-1][1]["json"]["deviceInfo"]["drmSupported"] is False
    finally:
        client.close()


def test_requested_audio_is_not_silently_substituted(video, monkeypatch):
    monkeypatch.setattr(ZDFEpisode, "_fetch", lambda self: video)
    episode = ZDFEpisode(ZDF_URL, selected_language="English Dub")
    with pytest.raises(BroadcasterError, match="standard audio"):
        episode.selected_format()


def test_naming_is_inside_target(video, monkeypatch, tmp_path):
    video.series = "../../unsafe:name"
    monkeypatch.setattr(ZDFEpisode, "_fetch", lambda self: video)
    monkeypatch.setenv(
        "H0MELAB_NAMING_TEMPLATE",
        "{title}/Season {season}/{title} S{season}E{episode}.mkv",
    )
    path = ZDFEpisode(ZDF_URL, selected_path=str(tmp_path))._episode_path
    assert path.is_relative_to(tmp_path)
    assert "S02E003" in path.name


@pytest.mark.parametrize(
    "cls,url,site", [(ZDFEpisode, ZDF_URL, "zdf"), (DMAXEpisode, DMAX_URL, "dmax")]
)
def test_web_url_to_episode_and_queue(client, monkeypatch, video, cls, url, site):
    monkeypatch.setattr(cls, "_fetch", lambda self: video)
    search = client.post("/api/search", json={"site": site, "keyword": url})
    assert search.status_code == 200
    assert search.json["results"][0]["url"] == url
    assert client.get("/api/series", query_string={"url": url}).status_code == 200
    assert (
        client.get("/api/seasons", query_string={"url": url}).json["seasons"][0][
            "season_number"
        ]
        == 2
    )
    episodes = client.get("/api/episodes", query_string={"url": url}).json["episodes"]
    assert episodes[0]["episode_number"] == 3
    assert client.get("/api/providers", query_string={"url": url}).json[
        "providers"
    ] == {"German Dub": [cls.name]}
    assert (
        client.post(
            "/api/download", json={"episodes": [url], "provider": cls.name}
        ).status_code
        == 200
    )


def test_http_transfer_verifies_before_publish(video, monkeypatch, tmp_path):
    from h0melab.models.broadcasters import transfer

    monkeypatch.setattr(ZDFEpisode, "_fetch", lambda self: video)
    episode = ZDFEpisode(ZDF_URL, selected_path=str(tmp_path))
    monkeypatch.setattr(
        transfer.common,
        "_download_http_file",
        lambda path, *a, **k: Path(path).write_bytes(b"input"),
    )
    monkeypatch.setattr(
        transfer.common,
        "_run_ffmpeg_with_progress",
        lambda *a, **k: episode._episode_path.with_suffix(".broadcast.mkv").write_bytes(
            b"verified"
        ),
    )
    monkeypatch.setattr(
        transfer.common,
        "check_downloaded",
        lambda path: {"exists": True, "video_langs": {"und"}, "audio_langs": {"deu"}},
    )
    monkeypatch.setattr(
        transfer.common,
        "_finalize_episode",
        lambda temp, output, *a, **k: temp.replace(output),
    )
    assert episode.download().read_bytes() == b"verified"
    assert not episode._episode_path.with_suffix(".broadcast.mp4").exists()


def test_pause_preserves_partial_http(video, monkeypatch, tmp_path):
    from h0melab.models.broadcasters import transfer

    monkeypatch.setattr(ZDFEpisode, "_fetch", lambda self: video)
    episode = ZDFEpisode(ZDF_URL, selected_path=str(tmp_path))

    def pause(path, *args, **kwargs):
        path.write_bytes(b"partial")
        raise transfer.common.DownloadPaused()

    monkeypatch.setattr(transfer.common, "_download_http_file", pause)
    with pytest.raises(transfer.common.DownloadPaused):
        episode.download()
    assert (
        episode._episode_path.with_suffix(".broadcast.mp4").read_bytes() == b"partial"
    )


def test_no_ytdlp_imports_in_native_modules():
    import ast

    import h0melab.models.broadcasters as package

    for path in Path(package.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                assert all(not alias.name.startswith("yt_dlp") for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("yt_dlp")


@pytest.mark.parametrize(
    "error", ["live playlist (no EXT-X-ENDLIST)", "encryption method SAMPLE-AES"]
)
def test_no_ffmpeg_fallback_for_live_or_protected_hls(
    video, monkeypatch, tmp_path, error
):
    from h0melab.models.broadcasters import transfer

    video.formats = [MediaFormat("https://cdn.example/master.m3u8", "hls")]
    monkeypatch.setattr(ZDFEpisode, "_fetch", lambda self: video)

    def unsupported(*args, **kwargs):
        raise transfer.hls.HLSUnsupported(error)

    monkeypatch.setattr(transfer.hls, "download_hls_parallel", unsupported)

    def unexpected(*args, **kwargs):
        pytest.fail("protected/live media reached FFmpeg")

    monkeypatch.setattr(transfer.common, "_run_ffmpeg_with_progress", unexpected)
    with pytest.raises(BroadcasterError, match="live or protected"):
        ZDFEpisode(ZDF_URL, selected_path=str(tmp_path)).download()


def test_reject_identity_changed_during_refresh(video, monkeypatch):
    episode = ZDFEpisode(ZDF_URL)
    episode.video = video
    replacement = Video("other", "Different video", formats=video.formats)
    monkeypatch.setattr(ZDFEpisode, "_fetch", lambda self: replacement)
    with pytest.raises(BroadcasterError, match="identity changed"):
        episode.download()


def test_invalid_file_never_published(video, monkeypatch, tmp_path):
    from h0melab.models.broadcasters import transfer

    monkeypatch.setattr(ZDFEpisode, "_fetch", lambda self: video)
    episode = ZDFEpisode(ZDF_URL, selected_path=str(tmp_path))
    monkeypatch.setattr(transfer.common, "_download_http_file", lambda *a, **k: None)
    monkeypatch.setattr(
        transfer.common, "_run_ffmpeg_with_progress", lambda *a, **k: None
    )
    with pytest.raises(BroadcasterError, match="verification"):
        episode.download()
    assert not episode._episode_path.exists()


def test_settings_and_source_capabilities(client):
    sources = client.get("/api/sources").json["sources"]
    for key in ("zdf", "dmax"):
        source = next(source for source in sources if source["id"] == key)
        assert source["input_mode"] == "text_or_url"
        assert source["text_search"] is True
        assert source["collections"] is True


def test_existing_other_language_is_never_overwritten(video, monkeypatch, tmp_path):
    from h0melab.models.broadcasters import transfer

    monkeypatch.setattr(ZDFEpisode, "_fetch", lambda self: video)
    episode = ZDFEpisode(ZDF_URL, selected_path=str(tmp_path))
    output = episode._episode_path
    output.parent.mkdir(parents=True)
    output.write_bytes(b"existing")
    monkeypatch.setattr(
        transfer.common,
        "check_downloaded",
        lambda path: {
            "exists": True,
            "video_langs": {"und"},
            "audio_langs": {"eng"},
            "subtitle_langs": set(),
        },
    )
    with pytest.raises(BroadcasterError, match="existing output"):
        episode.download()
    assert output.read_bytes() == b"existing"


def test_long_standalone_titles_keep_distinct_source_ids(monkeypatch, tmp_path):
    first = ZDFEpisode(ZDF_URL, selected_path=str(tmp_path))
    second = ZDFEpisode(ZDF_URL, selected_path=str(tmp_path))
    first.video = Video("first-id", "Very long title " * 40)
    second.video = Video("second-id", first.title)
    assert first._episode_path != second._episode_path
    assert len(first._episode_path.name) < 130


def test_series_names_drop_empty_metadata_and_preserve_episode_suffix(
    video, monkeypatch, tmp_path
):
    video.series = "A very long show title " * 15
    monkeypatch.setattr(DMAXEpisode, "_fetch", lambda self: video)
    monkeypatch.setenv(
        "H0MELAB_NAMING_TEMPLATE",
        "{title} ({year}) [imdbid-{imdbid}]/Season {season}/{title} S{season}E{episode}.mkv",
    )
    path = DMAXEpisode(DMAX_URL, selected_path=str(tmp_path))._episode_path
    assert "()" not in str(path)
    assert "[imdbid-]" not in str(path)
    assert path.name.endswith("S02E003.mkv")


@pytest.mark.parametrize(
    "protocol,text",
    [
        ("hls", "#EXTM3U\n#EXTINF:5,\nsegment.ts"),
        ("hls", '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"\n#EXT-X-ENDLIST'),
        ("dash", '<MPD type="dynamic"/>'),
        ("dash", "<MPD><Period><ContentProtection/></Period></MPD>"),
        (
            "dash",
            '<MPD><Period><AdaptationSet contentType="audio" lang="en"/></Period></MPD>',
        ),
    ],
)
def test_ffmpeg_fallback_checks_manifests(monkeypatch, protocol, text):
    from h0melab.models.broadcasters import transfer

    monkeypatch.setattr(transfer, "_manifest", lambda *args: text)
    with pytest.raises(BroadcasterError):
        transfer._fallback_inputs(
            MediaFormat("https://cdn.example/manifest", protocol), {}, "deu"
        )


def test_hls_fallback_selects_highest_video_and_requested_audio(monkeypatch):
    import ffmpeg

    from h0melab.models.broadcasters import transfer

    master = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",LANGUAGE="de",URI="de.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",LANGUAGE="en",URI="en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100,AUDIO="audio"
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000,AUDIO="audio"
high.m3u8
"""
    monkeypatch.setattr(
        transfer,
        "_manifest",
        lambda url, headers: (
            master if url.endswith("master.m3u8") else "#EXTM3U\n#EXT-X-ENDLIST"
        ),
    )
    inputs = transfer._fallback_inputs(
        MediaFormat("https://cdn.example/master.m3u8", "hls"), {}, "deu"
    )
    command = ffmpeg.compile(ffmpeg.output(*inputs, "output.mkv", c="copy"))
    assert "https://cdn.example/high.m3u8" in command
    assert "https://cdn.example/de.m3u8" in command
    assert "https://cdn.example/en.m3u8" not in command


def test_zdf_subtitle_choice_reaches_worker(client, monkeypatch, video):
    from h0melab.web import db

    monkeypatch.setattr(ZDFEpisode, "_fetch", lambda self: video)
    response = client.post(
        "/api/download",
        json={"episodes": [ZDF_URL], "provider": "ZDF", "subtitle_language": "deu"},
    )
    assert response.status_code == 200
    item = db.get_queue_item(response.json["queue_id"])
    _, episode = worker._build_episode(ZDF_URL, {}, dict(item), None)
    assert episode.selected_subtitle_language == "deu"
