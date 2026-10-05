"""Parallel HLS fetching and its integration into the shared downloader."""

import io
import time
from types import SimpleNamespace

import pytest

from h0melab.models.common import common, hls, transfer


@pytest.fixture(autouse=True)
def reset_progress(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise transfer.TransferUnavailable("disabled in compatibility-path test")

    monkeypatch.setattr(transfer, "download_with_ytdlp", unavailable)
    common._clear_download_progress()
    yield
    common._clear_download_progress()


def test_media_playlist_keeps_durations_for_truthful_progress():
    segments, init_uri = hls._parse_media_playlist(
        """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:7
#EXTINF:4.5,
first.ts
#EXTINF:5.5,
second.ts
#EXT-X-ENDLIST
""",
        "https://cdn.example/path/index.m3u8",
    )

    assert init_uri is None
    assert [segment.uri for segment in segments] == [
        "https://cdn.example/path/first.ts",
        "https://cdn.example/path/second.ts",
    ]
    assert [segment.sequence for segment in segments] == [7, 8]
    assert [segment.duration for segment in segments] == [4.5, 5.5]


def test_paused_playlist_resumes_after_the_last_flushed_segment(monkeypatch, tmp_path):
    segments = [
        hls._Segment(f"https://cdn.example/{name}.ts", None, index, 5.0)
        for index, name in enumerate(("one", "two", "three"), 1)
    ]
    playlist = hls._MediaPlaylist(
        "https://cdn.example/index.m3u8", segments, None
    )
    tracker = hls._ProgressTracker([playlist], "test")
    calls = []

    def pause_after_first(url, _headers, on_bytes=None, check_cancelled=None):
        calls.append(url)
        if url.endswith("two.ts"):
            raise common.DownloadPaused("Download paused")
        return url.rsplit("/", 1)[-1].encode()

    monkeypatch.setattr(hls, "_fetch_bytes", pause_after_first)
    prefix = tmp_path / "episode.hlswork"
    with pytest.raises(common.DownloadPaused):
        hls._download_playlist(playlist, {}, prefix, ".hls_video", tracker, 1)

    resumed_calls = []

    def resume(url, _headers, on_bytes=None, check_cancelled=None):
        resumed_calls.append(url)
        return url.rsplit("/", 1)[-1].encode()

    monkeypatch.setattr(hls, "_fetch_bytes", resume)
    tracker = hls._ProgressTracker([playlist], "test")
    output = hls._download_playlist(playlist, {}, prefix, ".hls_video", tracker, 1)

    assert resumed_calls == [
        "https://cdn.example/two.ts",
        "https://cdn.example/three.ts",
    ]
    assert output.read_bytes() == b"one.tstwo.tsthree.ts"


def test_parallel_download_selects_best_variant_and_requested_audio(
    monkeypatch, tmp_path
):
    playlists = {
        "https://cdn.example/master.m3u8": """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",LANGUAGE="de",NAME="Deutsch",URI="de.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=100,AUDIO="audio"
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=200,AUDIO="audio"
high.m3u8
""",
        "https://cdn.example/high.m3u8": """#EXTM3U
#EXTINF:5,
v1.ts
#EXTINF:5,
v2.ts
#EXT-X-ENDLIST
""",
        "https://cdn.example/de.m3u8": """#EXTM3U
#EXTINF:10,
a1.ts
#EXT-X-ENDLIST
""",
    }
    payloads = {
        "https://cdn.example/v1.ts": b"video-1",
        "https://cdn.example/v2.ts": b"video-2",
        "https://cdn.example/a1.ts": b"audio",
    }

    monkeypatch.setenv("H0MELAB_HLS_CONCURRENCY", "3")
    monkeypatch.setattr(hls, "_fetch_text", lambda url, _headers: playlists[url])

    def fetch(url, _headers, on_bytes=None, check_cancelled=None):
        if check_cancelled:
            check_cancelled()
        data = payloads[url]
        if on_bytes:
            time.sleep(0.02)
            on_bytes(len(data))
        return data

    monkeypatch.setattr(hls, "_fetch_bytes", fetch)

    written = hls.download_hls_parallel(
        "https://cdn.example/master.m3u8",
        tmp_path / "episode.work",
        preferred_audio_lang="deu",
        progress_end=85,
        keep_progress=True,
    )

    assert [path.read_bytes() for path in written] == [b"video-1video-2", b"audio"]
    progress = common.get_ffmpeg_progress()
    assert progress["percent"] == 85.0
    assert progress["time"] == "3/3 segments"
    assert progress["bandwidth"].endswith(" MB/s")
    assert progress["active"] is True


def test_video_only_does_not_fetch_a_separate_audio_rendition(monkeypatch, tmp_path):
    playlists = {
        "https://cdn.example/master.m3u8": """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",LANGUAGE="de",URI="de.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=200,AUDIO="audio"
video.m3u8
""",
        "https://cdn.example/video.m3u8": """#EXTM3U
#EXTINF:5,
v.ts
#EXT-X-ENDLIST
""",
    }
    requested = []
    monkeypatch.setenv("H0MELAB_HLS_CONCURRENCY", "2")

    def fetch_text(url, _headers):
        requested.append(url)
        return playlists[url]

    monkeypatch.setattr(hls, "_fetch_text", fetch_text)
    monkeypatch.setattr(
        hls,
        "_fetch_bytes",
        lambda *_args, **_kwargs: b"video",
    )

    written = hls.download_hls_parallel(
        "https://cdn.example/master.m3u8",
        tmp_path / "episode.work",
        include_audio=False,
    )

    assert len(written) == 1
    assert "https://cdn.example/de.m3u8" not in requested


@pytest.mark.parametrize(
    "page_url,stream_url,concurrency,expected",
    [
        (
            "https://aniworld.to/anime/stream/show/staffel-1/episode-1",
            "https://cdn.example/master.m3u8",
            "8",
            True,
        ),
        (
            "https://s.to/serie/stream/show/staffel-1/episode-1",
            "https://cdn.example/master.m3u8",
            "8",
            True,
        ),
        (
            "https://moflix-stream.xyz/titles/42",
            "https://cdn.example/master.m3u8",
            "8",
            True,
        ),
        (
            "https://moflix-stream.xyz/titles/42",
            "https://cdn.example/master.txt",
            "8",
            True,
        ),
        (
            "https://aniworld.to/anime/stream/show/staffel-1/episode-1",
            "https://cdn.example/video.mp4",
            "8",
            False,
        ),
        (
            "https://aniworld.to/anime/stream/show/staffel-1/episode-1",
            "https://cdn.example/master.m3u8",
            "1",
            False,
        ),
    ],
)
def test_parallel_hls_scope(monkeypatch, page_url, stream_url, concurrency, expected):
    monkeypatch.setenv("H0MELAB_HLS_CONCURRENCY", concurrency)
    owner = SimpleNamespace(url=page_url, selected_provider="MoflixClick")
    assert common._parallel_hls_enabled(owner, stream_url) is expected


def test_parallel_failure_cleans_up_and_falls_back(monkeypatch, tmp_path):
    cleaned = []

    def fail(*_args, **_kwargs):
        raise hls.HLSUnsupported("byte ranges")

    monkeypatch.setattr(hls, "download_hls_parallel", fail)
    monkeypatch.setattr(hls, "cleanup_temp_files", cleaned.append)

    result = common._try_parallel_hls(
        "https://cdn.example/master.m3u8",
        tmp_path / "episode.work",
        {},
        "deu",
        "Episode",
        include_audio=True,
        progress_end=85,
    )

    assert result is None
    assert cleaned == [tmp_path / "episode.work"]
    assert common.get_ffmpeg_progress()["active"] is False


def test_full_stream_uses_parallel_files_before_ffmpeg(monkeypatch, tmp_path):
    video = tmp_path / "video.ts"
    audio = tmp_path / "audio.ts"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    output = tmp_path / "episode.temp_full.mkv"
    runs = []

    monkeypatch.setattr(
        common,
        "_try_parallel_hls",
        lambda *_args, **_kwargs: [video, audio],
    )
    monkeypatch.setattr(
        common,
        "_run_ffmpeg_with_progress",
        lambda node, **kwargs: runs.append((node, kwargs)),
    )
    monkeypatch.setattr(hls, "cleanup_temp_files", lambda _path: None)

    used_parallel = common._download_full_stream(
        "https://cdn.example/master.m3u8",
        output,
        {},
        {},
        {"metadata:s:a:0": "language=deu"},
        "copy",
        "Episode",
        "deu",
        parallel_hls=True,
    )

    assert used_parallel is True
    assert len(runs) == 1
    assert runs[0][1]["progress_start"] == 85.0
    assert runs[0][1]["progress_end"] == 95.0
    assert runs[0][1]["keep_progress"] is True


def test_ffmpeg_output_growth_is_not_claimed_as_network_speed(monkeypatch):
    class Process:
        def __init__(self):
            self.stderr = io.BytesIO(
                b"Duration: 00:00:10.00\n"
                b"frame=1 size=1024kB time=00:00:05.00 bitrate=1 speed=1x\n"
            )
            self.returncode = 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = 1

        def terminate(self):
            self.returncode = 1

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(
        common.ffmpeg, "compile", lambda *_args, **_kwargs: ["ffmpeg", "out"]
    )
    monkeypatch.setattr(common.subprocess, "Popen", lambda *_args, **_kwargs: Process())

    common._run_ffmpeg_with_progress(object(), keep_progress=True)

    progress = common.get_ffmpeg_progress()
    assert progress["active"] is True
    assert progress["percent"] == 100.0
    assert progress["bandwidth"] == ""


def test_manual_hls_reports_measured_transfer_rate(monkeypatch, tmp_path):
    playlist = (
        "#EXTM3U\n#EXTINF:5,\nfirst.jpg\n#EXTINF:5,\nsecond.jpg\n#EXT-X-ENDLIST\n"
    )
    packet = b"\x47" + b"\x00" * 187

    class Response:
        text = playlist

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    observed = []

    def fetch(*_args, **_kwargs):
        observed.append(common.get_ffmpeg_progress()["bandwidth"])
        return packet * 2

    monkeypatch.setattr(common.niquests, "Session", Session)
    monkeypatch.setattr(common, "_fetch_hls_segment", fetch)
    output = tmp_path / "episode.seg.ts"

    common._download_hls_manual(
        "https://cdn.example/master.m3u8", {}, output, "Episode"
    )

    assert output.read_bytes() == packet * 4
    assert observed[1].endswith(" MB/s")
    assert common.get_ffmpeg_progress()["active"] is False


@pytest.mark.parametrize(
    "playlist,payload",
    [
        (
            "#EXTM3U\n#EXTINF:5,\nfirst.jpg\n#EXT-X-ENDLIST\n",
            b"<html>temporary CDN failure</html>",
        ),
        (
            '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:5,\nfirst.m4s\n#EXT-X-ENDLIST\n',
            None,
        ),
    ],
)
def test_manual_hls_rejects_non_ts_media(monkeypatch, tmp_path, playlist, payload):
    class Response:
        text = playlist

        def raise_for_status(self):
            pass

    class Session:
        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(common.niquests, "Session", Session)
    if payload is not None:
        monkeypatch.setattr(
            common, "_fetch_hls_segment", lambda *_args, **_kwargs: payload
        )
    output = tmp_path / "episode.seg.ts"

    with pytest.raises(common._HLSManualUnsupported):
        common._download_hls_manual("https://cdn.example/master.m3u8", {}, output)

    assert not output.exists()


def test_failed_local_hls_remux_retries_original_playlist(monkeypatch, tmp_path):
    output = tmp_path / "episode.temp_full.mkv"
    playlist = "https://cdn.example/master.m3u8"
    inputs = []

    def manual(_url, _headers, temp_ts, _label):
        temp_ts.write_bytes(b"bad transport stream")

    def ffmpeg_run(node, **_kwargs):
        args = common.ffmpeg.compile(node)
        inputs.append(args[args.index("-i") + 1])
        if len(inputs) == 1:
            output.write_bytes(b"partial output")
            raise RuntimeError("invalid input")
        assert not output.exists()

    monkeypatch.setattr(common, "_download_hls_manual", manual)
    monkeypatch.setattr(common, "_run_ffmpeg_with_progress", ffmpeg_run)

    used_parallel = common._download_full_stream(
        playlist, output, {}, {}, {}, "copy", "Episode", "deu"
    )

    assert used_parallel is False
    assert inputs == [str(output.with_suffix(".seg.ts")), playlist]
    assert not output.with_suffix(".seg.ts").exists()


def test_ytdlp_is_the_primary_full_stream_transfer(monkeypatch, tmp_path):
    output = tmp_path / "episode.temp_full.mkv"
    seen = {}

    def download(url, path, headers, concurrency, hook, preferred_audio_lang=None):
        seen.update(
            url=url,
            headers=headers,
            concurrency=concurrency,
            language=preferred_audio_lang,
        )
        path.write_bytes(b"media")
        hook(
            {
                "status": "downloading",
                "downloaded_bytes": 50,
                "total_bytes": 100,
                "speed": 20 * 1024 * 1024,
            }
        )

    monkeypatch.setattr(transfer, "download_with_ytdlp", download)
    monkeypatch.setattr(
        common,
        "_try_parallel_hls",
        lambda *_args, **_kwargs: pytest.fail("compatibility HLS must be second"),
    )

    staged = common._download_full_stream(
        "https://cdn.example/master.m3u8",
        output,
        {},
        {"Referer": "https://source.example/"},
        {},
        "copy",
        "Episode",
        "deu",
        parallel_hls=True,
    )

    assert staged is True
    assert seen == {
        "url": "https://cdn.example/master.m3u8",
        "headers": {"Referer": "https://source.example/"},
        "concurrency": 8,
        "language": "deu",
    }
    assert common.get_ffmpeg_progress()["percent"] == 95.0


def test_failed_parallel_remux_retries_original_playlist(monkeypatch, tmp_path):
    output = tmp_path / "episode.temp_full.mkv"
    playlist = "https://cdn.example/master.m3u8"
    segment = tmp_path / "episode.temp_full.hls_video.mp4"
    segment.write_bytes(b"bad fragment")
    inputs = []

    monkeypatch.setattr(
        common, "_try_parallel_hls", lambda *_args, **_kwargs: [segment]
    )
    monkeypatch.setattr(
        common,
        "_download_hls_manual",
        lambda *_args, **_kwargs: pytest.fail("must not redownload the same segments"),
    )

    def ffmpeg_run(node, **_kwargs):
        args = common.ffmpeg.compile(node)
        inputs.append(args[args.index("-i") + 1])
        if len(inputs) == 1:
            output.write_bytes(b"partial output")
            raise RuntimeError("invalid fragment")
        assert not output.exists()

    monkeypatch.setattr(common, "_run_ffmpeg_with_progress", ffmpeg_run)

    used_parallel = common._download_full_stream(
        playlist, output, {}, {}, {}, "copy", "Episode", "deu", parallel_hls=True
    )

    assert used_parallel is False
    assert inputs == [str(segment), playlist]
    assert not segment.exists()


def test_direct_http_reports_bytes_speed_and_scaled_progress(monkeypatch, tmp_path):
    chunk = b"x" * (1024 * 1024)

    class Response:
        def __init__(self):
            self.headers = {"Content-Length": str(len(chunk) * 2)}
            self.closed = False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            assert chunk_size == 1024 * 1024
            return iter((chunk, chunk))

        def close(self):
            self.closed = True

    response = Response()
    monkeypatch.setattr(common.niquests, "get", lambda *_args, **_kwargs: response)
    ticks = iter((0.0, 1.0, 2.0))
    monkeypatch.setattr(common.time, "monotonic", lambda: next(ticks))
    output = tmp_path / "source.mp4"

    common._download_http_file(
        output,
        "https://cdn.example/video.mp4",
        headers={"Referer": "https://example.com/"},
        progress_end=90,
        keep_progress=True,
    )

    progress = common.get_ffmpeg_progress()
    assert output.stat().st_size == len(chunk) * 2
    assert response.closed is True
    assert progress["percent"] == 90
    assert progress["time"] == "2.0/2.0 MB"
    assert progress["bandwidth"] == "1.0 MB/s"
    assert progress["active"] is True


def test_full_stream_stages_direct_http_before_local_remux(monkeypatch, tmp_path):
    output = tmp_path / "episode.temp_full.mkv"
    downloads = []
    remuxes = []

    def download(path, url, **kwargs):
        path.write_bytes(b"source")
        downloads.append((path, url, kwargs))

    def remux(node, **kwargs):
        args = common.ffmpeg.compile(node)
        remuxes.append((args[args.index("-i") + 1], kwargs))

    monkeypatch.setattr(common, "_download_http_file", download)
    monkeypatch.setattr(common, "_run_ffmpeg_with_progress", remux)

    staged = common._download_full_stream(
        "https://edge.veevcdn.co/signed/video",
        output,
        {},
        {"Referer": "https://veev.to/"},
        {"metadata:s:a:0": "language=deu"},
        "copy",
        "Movie",
        "deu",
        direct_http=True,
    )

    source = output.with_suffix(".direct.mp4")
    assert staged is True
    assert downloads[0][0] == source
    assert downloads[0][2]["progress_end"] == 90.0
    assert remuxes == [
        (
            str(source),
            {
                "label": "Movie",
                "progress_start": 90.0,
                "progress_end": 95.0,
                "keep_progress": True,
            },
        )
    ]
    assert not source.exists()
