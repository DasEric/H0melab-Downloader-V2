"""Moflix movie/series routes and the hosters they actually expose."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from h0melab.extractors.provider import gupload, moflixclick, veev, vidara
from h0melab.models.common.provider_map import host_to_provider
from h0melab.models.moflix_stream import series as moflix
from h0melab.search import fetch_moflix_movies, query_moflix


def _response(*, payload=None, text="", status_code=200):
    def raise_for_status():
        if status_code >= 400:
            raise RuntimeError(f"HTTP {status_code}")

    return SimpleNamespace(
        status_code=status_code,
        cookies={},
        text=text,
        json=lambda: payload,
        raise_for_status=raise_for_status,
        iter_content=lambda chunk_size=512: iter((b"media bytes",)),
        close=lambda: None,
    )


def _moflix_api(monkeypatch, *, series=False):
    calls = []
    title = {
        "name": "Sample",
        "year": 2026,
        "poster": "/poster.jpg",
        "is_series": series,
    }
    videos = [
        {"name": "Mirror 1", "src": "https://gupload.xyz/e/first"},
        {"name": "Mirror 2", "src": "https://moflix-stream.click/embed/second"},
        {"name": "Mirror 3", "src": "https://moflix.upns.xyz/e/not-voe"},
        {"name": "Mirror 4", "src": "https://streamtape.com/e/unimplemented"},
    ]

    def fetch(url, *args, **kwargs):
        calls.append(url)
        if "/api/v1/titles/42/seasons/1/episodes/" in url:
            return _response(payload={"episode": {"videos": videos}})
        if "/api/v1/titles/42/seasons/1?" in url:
            return _response(
                payload={
                    "episodes": {
                        "data": [
                            {"episode_number": 1, "name": "One"},
                            {"episode_number": 2, "name": "Two"},
                        ]
                    }
                }
            )
        if url.endswith("/api/v1/titles/42"):
            return _response(
                payload={
                    "title": {**title, "videos": [] if series else videos},
                    "seasons": {"data": [{"number": 1, "episodes_count": 2}]}
                    if series
                    else {},
                }
            )
        return _response(text='window.bootstrapData = {"csrf_token":"test"};')

    monkeypatch.setattr(moflix, "_fetch_moflix", fetch)
    return calls


def test_movie_routes_include_only_implemented_hosters(client, monkeypatch):
    _moflix_api(monkeypatch)
    url = "https://moflix-stream.xyz/titles/42"
    title = client.get("/api/series", query_string={"url": url}).get_json()
    assert title["title"] == "Sample"
    assert title["poster_url"]

    seasons = client.get("/api/seasons", query_string={"url": url}).get_json()[
        "seasons"
    ]
    assert len(seasons) == 1 and seasons[0]["are_movies"] is True
    episodes = client.get("/api/episodes", query_string={"url": url}).get_json()[
        "episodes"
    ]
    assert len(episodes) == 1 and episodes[0]["episode_number"] == 1
    providers = client.get("/api/providers", query_string={"url": url}).get_json()[
        "providers"
    ]
    assert providers == {"German Dub": ["Gupload", "MoflixClick"]}

    movie = moflix.MoflixEpisode(url)
    season = movie.seasons[0]
    episode = season.episodes[0]
    assert isinstance(episode, moflix.MoflixEpisode)
    assert episode.series is movie
    assert episode.season is season
    assert episode.episode_number == 1


def test_series_listing_does_not_probe_every_episode(client, monkeypatch):
    calls = _moflix_api(monkeypatch, series=True)
    url = "https://moflix-stream.xyz/titles/42?season=1"
    response = client.get("/api/episodes", query_string={"url": url})
    assert response.status_code == 200
    episodes = response.get_json()["episodes"]
    assert [episode["episode_number"] for episode in episodes] == [1, 2]
    assert all(episode["available_languages"] == ["German Dub"] for episode in episodes)
    assert not any("/episodes/" in call for call in calls)

    series = moflix.MoflixEpisode(url)
    season = series.seasons[0]
    episode_models = season.episodes
    assert season.episodes is episode_models
    assert all(isinstance(episode, moflix.MoflixEpisode) for episode in episode_models)
    assert all(episode.series is series for episode in episode_models)
    assert all(episode.season is season for episode in episode_models)
    assert [episode.title_en for episode in episode_models] == ["One", "Two"]
    assert not any("/episodes/" in call for call in calls)

    providers = client.get("/api/providers", query_string={"url": episodes[0]["url"]})
    assert providers.get_json()["providers"] == {
        "German Dub": ["Gupload", "MoflixClick"]
    }
    download = moflix.MoflixEpisode(episodes[1]["url"])
    assert download._folder_path.name == "Season 1"
    assert "S1E2" in download._file_name


def test_mirror_names_are_not_mistaken_for_other_hosters():
    assert host_to_provider("gupload.xyz") == "Gupload"
    assert host_to_provider("moflix-stream.click") == "MoflixClick"
    assert host_to_provider("veev.to") == "Veev"
    assert host_to_provider("vidara.to") == "Vidara"
    assert host_to_provider("moflix.upns.xyz") is None
    assert host_to_provider("streamtape.com") is None


def test_moflix_fallback_uses_the_matching_extractor(monkeypatch):
    _moflix_api(monkeypatch)
    episode = moflix.MoflixEpisode(
        "https://moflix-stream.xyz/titles/42", selected_provider="MoflixClick"
    )
    assert episode.provider_attempt_order() == ("MoflixClick", "Gupload")
    assert episode.provider_url == "https://moflix-stream.click/embed/second"
    episode.selected_provider = "Gupload"
    assert episode.provider_url == "https://gupload.xyz/e/first"
    episode.selected_provider = "VOE"
    with pytest.raises(ValueError, match="No Moflix link for provider VOE"):
        _ = episode.provider_url


def test_moflix_download_falls_back_after_unreachable_hls(monkeypatch):
    from h0melab.models.common import common

    _moflix_api(monkeypatch)
    episode = moflix.MoflixEpisode(
        "https://moflix-stream.xyz/titles/42", selected_provider="MoflixClick"
    )
    calls = []

    def unreachable(url):
        calls.append(("MoflixClick", url))
        raise ValueError("MoflixClick has no reachable HLS playlist")

    def usable(url):
        calls.append(("Gupload", url))
        return "https://cdn.example/master.m3u8"

    monkeypatch.setattr(moflixclick, "get_direct_links_from_moflixclick", unreachable)
    monkeypatch.setitem(
        moflix.provider_functions, "get_direct_link_from_gupload", usable
    )
    monkeypatch.setattr(common.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        common,
        "check_downloaded",
        lambda _path: {"exists": True, "audio_langs": {"deu"}, "video_langs": {"und"}},
    )

    episode.download()

    assert calls == [
        ("MoflixClick", "https://moflix-stream.click/embed/second"),
        ("Gupload", "https://gupload.xyz/e/first"),
    ]
    assert episode.selected_provider == "Gupload"


def test_single_moflix_provider_failure_explains_missing_fallback(monkeypatch):
    from h0melab.models.common import common

    _moflix_api(monkeypatch)
    episode = moflix.MoflixEpisode(
        "https://moflix-stream.xyz/titles/42", selected_provider="MoflixClick"
    )
    monkeypatch.setattr(episode, "available_providers", lambda: ("MoflixClick",))

    def unavailable(_url):
        raise ValueError("no reachable HLS playlist")

    monkeypatch.setattr(moflixclick, "get_direct_links_from_moflixclick", unavailable)
    monkeypatch.setattr(common.platform, "system", lambda: "Linux")

    with pytest.raises(RuntimeError, match="No other supported provider is available"):
        episode.download()


def test_gupload_decodes_player_configuration(monkeypatch):
    key = b"G7#kP!2qZxV9mRwL"
    stream = "https://gupload.xyz/data/e/hls/sample/720p.m3u8"
    plain = json.dumps({"videoUrl": stream}).encode()
    encoded = base64.b64encode(
        bytes(value ^ key[i % len(key)] for i, value in enumerate(plain))
    ).decode()
    html = (
        "var _k=(function(){var _p=['G7#k','P!2q','ZxV9','mRwL'];"
        "return _p[0]+_p[1]+_p[2]+_p[3];})();"
        f"var _cfg = _dp('abc~{encoded}');"
    )
    monkeypatch.setattr(gupload.requests, "get", lambda *a, **k: _response(text=html))
    assert (
        gupload.get_direct_link_from_gupload("https://gupload.xyz/e/sample") == stream
    )


def test_moflixclick_unpacks_hls_links(monkeypatch):
    stream = "https://cdn.example/master.m3u8"
    html = (
        "eval(function(p,a,c,k,e,d){return p}('"
        f'1 0={{"2":"{stream}"}};'
        "',3,3,'links|var|hls2'.split('|')))"
    )

    def get(url, **kwargs):
        return _response(
            text=(
                html
                if url.endswith("/embed/sample")
                else "#EXTM3U\n#EXTINF:5,\nsegment.ts\n#EXT-X-ENDLIST\n"
            )
        )

    monkeypatch.setattr(moflixclick.requests, "get", get)
    assert (
        moflixclick.get_direct_link_from_moflixclick(
            "https://moflix-stream.click/embed/sample"
        )
        == stream
    )


def test_moflixclick_tries_the_next_reachable_playlist(monkeypatch):
    first = "https://cdn.example/first.txt"
    second = "https://cdn.example/second.m3u8"
    html = (
        "eval(function(p,a,c,k,e,d){return p}('"
        f'1 0={{"2":"{first}","3":"{second}"}};'
        "',4,4,'links|var|hls3|hls2'.split('|')))"
    )
    requested = []

    def get(url, **kwargs):
        requested.append(url)
        if "moflix-stream.click" in url:
            return _response(text=html)
        if url == first:
            raise TimeoutError("playlist timed out")
        if url == second:
            return _response(text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nvideo.m3u8")
        return _response(text="#EXTM3U\n#EXTINF:5,\nsegment.ts\n#EXT-X-ENDLIST\n")

    monkeypatch.setattr(moflixclick.requests, "get", get)
    assert (
        moflixclick.get_direct_link_from_moflixclick(
            "https://moflix-stream.click/embed/example"
        )
        == second
    )
    assert requested == [
        "https://moflix-stream.click/embed/example",
        first,
        second,
        "https://cdn.example/video.m3u8",
        "https://cdn.example/segment.ts",
    ]


@pytest.mark.parametrize("broken_child", ["video", "audio"])
def test_moflixclick_skips_master_with_invalid_child_playlist(
    monkeypatch, broken_child
):
    first = "https://cdn.example/first/master.m3u8"
    second = "https://cdn.example/second/master.txt"
    html = (
        "eval(function(p,a,c,k,e,d){return p}('"
        f'1 0={{"2":"{first}","3":"{second}"}};'
        "',4,4,'links|var|hls4|hls3'.split('|')))"
    )
    master = (
        '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="dub",LANGUAGE="de",'
        'URI="audio.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=100,AUDIO="dub"\n'
        "video.m3u8\n"
    )
    media = "#EXTM3U\n#EXTINF:5,\nsegment.ts\n#EXT-X-ENDLIST\n"
    requested = []

    def get(url, **_kwargs):
        requested.append(url)
        if url.endswith("/embed/example"):
            return _response(text=html)
        if url in (first, second):
            return _response(text=master)
        if url == f"https://cdn.example/first/{broken_child}.m3u8":
            return _response(text="<html>expired or denied</html>")
        return _response(text=media)

    monkeypatch.setattr(moflixclick.requests, "get", get)

    assert moflixclick.get_direct_links_from_moflixclick(
        "https://moflix-stream.click/embed/example"
    ) == (second,)
    assert f"https://cdn.example/first/{broken_child}.m3u8" in requested


def test_moflixclick_skips_master_with_unreachable_first_segment(monkeypatch):
    first = "https://cdn.example/first/master.m3u8"
    second = "https://cdn.example/second/master.m3u8"
    html = (
        "eval(function(p,a,c,k,e,d){return p}('"
        f'1 0={{"2":"{first}","3":"{second}"}};'
        "',4,4,'links|var|hls4|hls3'.split('|')))"
    )
    media = "#EXTM3U\n#EXTINF:5,\nsegment.ts\n#EXT-X-ENDLIST\n"
    requested = []

    def get(url, **_kwargs):
        requested.append(url)
        if url.endswith("/embed/example"):
            return _response(text=html)
        if url in (first, second):
            return _response(text=media)
        if url == "https://cdn.example/first/segment.ts":
            return _response(status_code=522)
        return _response()

    monkeypatch.setattr(moflixclick.requests, "get", get)

    assert moflixclick.get_direct_links_from_moflixclick(
        "https://moflix-stream.click/embed/example"
    ) == (second,)
    assert "https://cdn.example/first/segment.ts" in requested


def test_moflixclick_reports_when_every_mirror_has_dead_segments(monkeypatch):
    stream = "https://cdn.example/master.m3u8"
    html = (
        "eval(function(p,a,c,k,e,d){return p}('"
        f'1 0={{"2":"{stream}"}};'
        "',3,3,'links|var|hls4'.split('|')))"
    )

    def get(url, **_kwargs):
        if url.endswith("/embed/example"):
            return _response(text=html)
        if url == stream:
            return _response(text="#EXTM3U\n#EXTINF:5,\nsegment.ts\n")
        return _response(status_code=522)

    monkeypatch.setattr(moflixclick.requests, "get", get)

    with pytest.raises(ValueError, match=r"hls4: media segment returned HTTP 522"):
        moflixclick.get_direct_links_from_moflixclick(
            "https://moflix-stream.click/embed/example"
        )


def test_moflixclick_returns_all_usable_player_mirrors(monkeypatch):
    first = "https://cdn.example/first.m3u8"
    second = "https://cdn.example/second.m3u8"
    html = (
        "eval(function(p,a,c,k,e,d){return p}('"
        f'1 0={{"2":"{first}","3":"{second}"}};'
        "',4,4,'links|var|hls4|hls3'.split('|')))"
    )
    media = "#EXTM3U\n#EXTINF:5,\nsegment.ts\n#EXT-X-ENDLIST\n"
    monkeypatch.setattr(
        moflixclick.requests,
        "get",
        lambda url, **_kwargs: _response(
            text=html if url.endswith("/embed/example") else media
        ),
    )

    assert moflixclick.get_direct_links_from_moflixclick(
        "https://moflix-stream.click/embed/example"
    ) == (first, second)


def test_moflix_download_tries_next_hls_mirror_after_runtime_failure(
    monkeypatch, tmp_path
):
    from h0melab.models.common import common

    _moflix_api(monkeypatch)
    episode = moflix.MoflixEpisode(
        "https://moflix-stream.xyz/titles/42", selected_provider="MoflixClick"
    )
    episode.selected_path = str(tmp_path)
    first = "https://cdn.example/first.m3u8"
    second = "https://cdn.example/second.m3u8"
    monkeypatch.setattr(episode, "stream_url_candidates", lambda: (first, second))
    monkeypatch.setattr(common.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        common,
        "check_downloaded",
        lambda _path: {"exists": False, "audio_langs": set(), "video_langs": set()},
    )
    attempted = []

    def download_stream(url, output, *_args, **_kwargs):
        attempted.append(url)
        if url == first:
            raise RuntimeError("child playlist became invalid")
        output.write_bytes(b"downloaded")
        return False

    monkeypatch.setattr(common, "_download_full_stream", download_stream)
    monkeypatch.setattr(common, "_finalize_episode", lambda *_args, **_kwargs: None)

    episode.download()

    assert attempted == [first, second]
    assert episode.selected_provider == "MoflixClick"


def test_moflix_download_uses_other_provider_after_all_hls_mirrors_fail(
    monkeypatch, tmp_path
):
    from h0melab.models.common import common
    from h0melab.playwright import captcha
    from h0melab.web import db

    _moflix_api(monkeypatch)
    episode = moflix.MoflixEpisode(
        "https://moflix-stream.xyz/titles/42", selected_provider="MoflixClick"
    )
    episode.selected_path = str(tmp_path)
    mirrors = (
        "https://cdn.example/first.m3u8",
        "https://cdn.example/second.m3u8",
    )
    gupload_url = "https://cdn.example/gupload.m3u8"
    monkeypatch.setattr(episode, "stream_url_candidates", lambda: mirrors)
    monkeypatch.setitem(
        moflix.provider_functions,
        "get_direct_link_from_gupload",
        lambda _url: gupload_url,
    )
    monkeypatch.setattr(common.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        common,
        "check_downloaded",
        lambda _path: {"exists": False, "audio_langs": set(), "video_langs": set()},
    )
    attempted = []

    def download_stream(url, output, *_args, **_kwargs):
        attempted.append(url)
        if url in mirrors:
            raise RuntimeError("media segment unavailable")
        output.write_bytes(b"downloaded")
        return False

    monkeypatch.setattr(common, "_download_full_stream", download_stream)
    monkeypatch.setattr(common, "_finalize_episode", lambda *_args, **_kwargs: None)

    queue_id = db.add_to_queue(
        title="Sample",
        series_url=episode.url,
        episodes=[episode.url],
        language="German Dub",
        provider="MoflixClick",
    )
    captcha._local.queue_id = queue_id
    try:
        episode.download()
    finally:
        captcha._local.queue_id = None

    assert attempted == [*mirrors, gupload_url]
    assert episode.selected_provider == "Gupload"
    assert db.get_queue_item(queue_id)["active_provider"] == "Gupload"


@pytest.mark.parametrize(
    "player_link,expected",
    [
        ("//cdn.example/master.m3u8", "https://cdn.example/master.m3u8"),
        ("/hls/master.m3u8", "https://moflix-stream.click/hls/master.m3u8"),
        ("master.m3u8", "https://moflix-stream.click/embed/master.m3u8"),
    ],
)
def test_moflixclick_resolves_relative_playlist_links(
    monkeypatch, player_link, expected
):
    html = (
        "eval(function(p,a,c,k,e,d){return p}('"
        f'1 0={{"2":"{player_link}"}};'
        "',3,3,'links|var|hls4'.split('|')))"
    )
    requested = []

    def get(url, **kwargs):
        requested.append(url)
        return _response(
            text=(
                html
                if url.endswith("/embed/example")
                else "#EXTM3U\n#EXTINF:5,\nsegment.ts\n#EXT-X-ENDLIST\n"
            )
        )

    monkeypatch.setattr(moflixclick.requests, "get", get)
    assert (
        moflixclick.get_direct_link_from_moflixclick(
            "https://moflix-stream.click/embed/example"
        )
        == expected
    )
    assert requested == [
        "https://moflix-stream.click/embed/example",
        expected,
        expected.rsplit("/", 1)[0] + "/segment.ts",
    ]


def test_moflix_search_excludes_people_and_invalid_ids(monkeypatch):
    from h0melab.models.moflix_stream import client

    def get(url, **kwargs):
        if url.endswith("/search/silo"):
            return _response(
                text="window.bootstrapData = "
                + json.dumps(
                    {
                        "loaders": {
                            "searchPage": {
                                "query": "silo",
                                "results": [
                                    {
                                        "id": 42,
                                        "name": "Silo",
                                        "model_type": "title",
                                        "poster": "https://image.example/silo.jpg",
                                    },
                                    {
                                        "id": 101,
                                        "name": "Silo Septiadi",
                                        "model_type": "person",
                                        "poster": None,
                                    },
                                    {
                                        "id": None,
                                        "name": "Broken",
                                        "model_type": "title",
                                        "poster": None,
                                    },
                                ],
                            }
                        }
                    }
                )
                + ";"
            )
        return _response(text='{"csrf_token":"test"}')

    monkeypatch.setattr(client, "_session", lambda: SimpleNamespace(get=get))
    assert query_moflix("silo") == [
        {
            "title": "Silo",
            "url": "https://moflix-stream.xyz/titles/42",
            "poster_url": "https://image.example/silo.jpg",
        }
    ]


def test_moflix_search_and_browse_propagate_request_errors(monkeypatch):
    from h0melab.models.moflix_stream import client

    def fail(*args, **kwargs):
        raise TimeoutError("moflix timed out")

    monkeypatch.setattr(client, "_session", lambda: SimpleNamespace(get=fail))

    with pytest.raises(TimeoutError, match="moflix timed out"):
        query_moflix("silo")
    with pytest.raises(TimeoutError, match="moflix timed out"):
        fetch_moflix_movies()


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://moflix-stream.xyz/titles/42/silo",
            "https://moflix-stream.xyz/titles/42",
        ),
        (
            "http://www.moflix-stream.xyz/titles/42/silo/?season=2",
            "https://moflix-stream.xyz/titles/42?season=2",
        ),
        (
            "https://moflix-stream.xyz/titles/42/silo/season/2/episodes/3",
            "https://moflix-stream.xyz/titles/42/season/2/episodes/3",
        ),
        (
            "https://moflix-stream.xyz/titles/42/season/2/episodes/3",
            "https://moflix-stream.xyz/titles/42/season/2/episodes/3",
        ),
        (
            "HTTPS://MOFLIX-STREAM.XYZ/TITLES/42/SEASON/2/EPISODES/3",
            "https://moflix-stream.xyz/titles/42/season/2/episodes/3",
        ),
    ],
)
def test_browser_and_legacy_urls_resolve_to_same_model(url, expected):
    from h0melab.providers import normalize_url, resolve_provider

    assert normalize_url(url) == expected
    assert resolve_provider(url).name == "Moflix"
    episode = moflix.MoflixEpisode(url)
    assert episode.url == expected
    assert episode.title_id == "42"


@pytest.mark.parametrize(
    "url",
    [
        "https://moflix-stream.xyz.evil.test/titles/42/silo",
        "https://example.test/titles/42/silo",
        "https://moflix-stream.xyz/titles/42/silo/unexpected",
        "https://moflix-stream.xyz/titles/42/season/nope",
        "https://moflix-stream.xyz/titles/42/episodes/3",
        "https://moflix-stream.xyz/titles/42/season?season=1",
    ],
)
def test_moflix_rejects_unrelated_or_malformed_urls(url):
    with pytest.raises(ValueError, match="Invalid Moflix"):
        moflix.MoflixEpisode(url)


def test_initial_model_bootstrap_handles_multiline_data(monkeypatch):
    _moflix_api(monkeypatch)
    original_fetch = moflix._fetch_moflix

    def fetch(url, *args):
        if "/api/" not in url:
            return _response(
                text='window.bootstrapData = {\n"csrf_token": "test",\n"nested": {"value": "};"}\n};'
            )
        return original_fetch(url, *args)

    monkeypatch.setattr(moflix, "_fetch_moflix", fetch)
    assert (
        moflix.MoflixEpisode("https://moflix-stream.xyz/titles/42/silo").title
        == "Sample"
    )


def test_episode_pagination_is_complete_deduplicated_and_lazy(monkeypatch):
    _moflix_api(monkeypatch, series=True)
    original_get = moflix.MoflixEpisode._api_get
    calls = []

    def get(self, url):
        calls.append(url)
        if "/seasons/1?" in url:
            if "page=2" in url:
                return _response(
                    payload={
                        "episodes": {
                            "data": [
                                {"episode_number": 1, "name": "One"},
                                {"episode_number": 2, "name": "Two"},
                            ],
                            "next_page": None,
                        }
                    }
                )
            return _response(
                payload={
                    "episodes": {
                        "data": [{"episode_number": 1, "name": "One"}],
                        "next_page": 2,
                    }
                }
            )
        return original_get(self, url)

    monkeypatch.setattr(moflix.MoflixEpisode, "_api_get", get)
    series = moflix.MoflixEpisode("https://moflix-stream.xyz/titles/42?season=1")
    episodes = series.seasons[0].episodes
    assert [ep.episode_number for ep in episodes] == [1, 2]
    assert episodes[1].title_en == "Two"
    assert not any("/episodes/" in url for url in calls)
    assert sum("/seasons/1?" in url for url in calls) == 2


def test_vidara_uses_its_stream_api(monkeypatch):
    class Session:
        def get(self, url, **kwargs):
            assert url == "https://vidara.to/e/sample123"
            return _response(text="<html></html>")

        def post(self, url, *, json, headers, **kwargs):
            assert url == "https://vidara.to/api/stream"
            assert json == {"filecode": "sample123", "device": "web"}
            assert headers["Referer"] == "https://vidara.to/e/sample123"
            return _response(
                payload={"streaming_url": "https://cdn.example/master.m3u8"}
            )

    monkeypatch.setattr(vidara.requests, "Session", lambda **kwargs: Session())
    assert (
        vidara.get_direct_link_from_vidara("https://vidara.to/e/sample123")
        == "https://cdn.example/master.m3u8"
    )


def test_veev_uses_player_handshake(monkeypatch):
    from h0melab.playwright import captcha

    direct = "https://edge.veevcdn.co/signed/video"
    monkeypatch.setattr(captcha, "playwright_get_veev_stream_url", lambda _url: direct)

    assert veev.get_direct_link_from_veev("https://veev.to/e/sample") == direct


def test_veev_uses_headless_browser_without_linux_display(monkeypatch):
    from patchright import sync_api

    from h0melab import autodeps
    from h0melab.playwright import captcha

    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(autodeps, "PLATFORM", "Linux")
    ensure_xvfb = Mock()
    monkeypatch.setattr(autodeps, "_ensure_xvfb", ensure_xvfb)

    runtime = MagicMock()
    playwright = MagicMock()
    playwright.return_value.__enter__.return_value = runtime
    monkeypatch.setattr(sync_api, "sync_playwright", playwright)

    def launch(_runtime, **kwargs):
        assert kwargs == {"offscreen": False, "headless": True}
        raise RuntimeError("capture stopped after launch assertion")

    monkeypatch.setattr(captcha, "_launch_browser_context", launch)

    with pytest.raises(RuntimeError, match="capture stopped after launch assertion"):
        captcha.playwright_get_veev_stream_url("https://veev.to/e/sample")

    ensure_xvfb.assert_called_once_with()


@pytest.mark.parametrize("url", ["http://veev.to/e/sample", "https://example.com/e/x"])
def test_veev_rejects_untrusted_embed_urls(url):
    with pytest.raises(ValueError, match="Invalid Veev embed URL"):
        veev.get_direct_link_from_veev(url)


@pytest.mark.parametrize("provider", ["Gupload", "MoflixClick", "Veev", "Vidara"])
def test_moflix_provider_headers_avoid_encoded_playlists(provider):
    from h0melab.config import PROVIDER_HEADERS_D

    if provider in {"Gupload", "MoflixClick", "Veev"}:
        assert PROVIDER_HEADERS_D[provider]["Accept-Encoding"] == "identity"
