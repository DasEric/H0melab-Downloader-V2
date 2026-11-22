"""Current public broadcaster search, catalogue and queue contracts."""

import pytest

from h0melab.models.broadcasters.client import BroadcasterError
from h0melab.models.broadcasters.dmax import (
    DMAX_COLLECTION_PATTERN,
    DMAX_PATTERN,
    DMAXClient,
    DMAXCollection,
)
from h0melab.models.broadcasters.zdf import ZDFClient
from h0melab.web import sitesearch

SHOW = "https://dmax.de/sendungen/test-show"


def item(uid, season=1, episode=1):
    return {
        "id": str(uid),
        "attributes": {
            "alternateId": f"season-{season}-episode-{episode}",
            "name": f"Episode {episode}",
            "seasonNumber": season,
            "episodeNumber": episode,
        },
        "relationships": {"show": {"data": {"id": "42"}}},
    }


@pytest.fixture
def catalogue(monkeypatch):
    data = {"title": "Test Show", "attributes": {"showId": "42"}}
    rows = [item(1), item(2, episode=3), item(3, season=2)]
    monkeypatch.setattr(DMAXClient, "show", lambda *a: (data, "42", "test-show"))
    monkeypatch.setattr(DMAXClient, "videos", lambda *a: rows)
    return rows


@pytest.mark.parametrize(
    "url", [SHOW, "https://dmax.de/test-show", SHOW + "?staffel=2"]
)
def test_show_and_episode_patterns_do_not_overlap(url):
    assert DMAX_COLLECTION_PATTERN.fullmatch(url)
    assert not DMAX_PATTERN.fullmatch(url)
    episode = url.split("?")[0] + "/season-1-episode-1"
    assert DMAX_PATTERN.fullmatch(episode)
    assert not DMAX_COLLECTION_PATTERN.fullmatch(episode)


def test_dmax_collection_preserves_numbering_and_season_filter(catalogue):
    collection = DMAXCollection(SHOW)
    assert [s["episode_count"] for s in collection.season_rows()] == [2, 1]
    assert [
        r["episode_number"] for r in DMAXCollection(SHOW + "?staffel=1").episode_rows()
    ] == [1, 3]
    with pytest.raises(BroadcasterError, match="no available"):
        DMAXCollection(SHOW + "?staffel=99").episode_rows()


def test_dmax_web_show_to_episode(client, catalogue):
    response = client.post("/api/search", json={"site": "dmax", "keyword": SHOW})
    assert response.status_code == 200
    assert response.json["results"][0]["title"] == "Test Show"
    assert client.get("/api/series", query_string={"url": SHOW}).status_code == 200
    seasons = client.get("/api/seasons", query_string={"url": SHOW}).json["seasons"]
    episodes = client.get(
        "/api/episodes", query_string={"url": seasons[0]["url"]}
    ).json["episodes"]
    assert [e["episode_number"] for e in episodes] == [1, 3]
    assert (
        client.post(
            "/api/download",
            json={"episodes": [e["url"] for e in episodes], "provider": "DMAX"},
        ).status_code
        == 200
    )


def test_dmax_catalogue_paginates(monkeypatch):
    calls = []

    def request(self, url, **kwargs):
        if url.endswith("token"):
            return {"data": {"attributes": {"token": "test"}}}
        page = kwargs["params"]["page[number]"]
        calls.append(page)
        return {"data": [item(page)], "meta": {"totalPages": 2}}

    monkeypatch.setattr(DMAXClient, "request", request)
    c = DMAXClient()
    try:
        assert [v["id"] for v in c.videos("42")] == ["1", "2"]
        assert calls == [1, 2]
    finally:
        c.close()


@pytest.mark.parametrize(
    "response",
    [
        {"data": [], "meta": {}},
        {"data": [], "meta": {"totalPages": 2}},
        {"data": [item(1)], "meta": {"totalPages": 2}},
    ],
)
def test_dmax_incomplete_or_looping_catalogue_is_error(monkeypatch, response):
    monkeypatch.setattr(DMAXClient, "headers", lambda self: {})
    monkeypatch.setattr(DMAXClient, "request", lambda *a, **k: response)
    c = DMAXClient()
    try:
        with pytest.raises(BroadcasterError):
            c.videos("42")
    finally:
        c.close()


def test_dmax_search_uses_next_page_not_total_pages(monkeypatch):
    calls = []

    def site(self, path, **params):
        page = params["page[number]"]
        calls.append(page)
        return {
            "meta": {"totalPages": 0, "nextPage": 2 if page == 1 else None},
            "data": [
                {"type": "showpage", "title": "Show", "slug": f"show-{page}"},
                {"type": "article", "title": "Skip", "slug": "article"},
            ],
        }

    monkeypatch.setattr(DMAXClient, "site", site)
    c = DMAXClient()
    try:
        assert len(c.search("show")) == 2
        assert calls == [1, 2]
    finally:
        c.close()


def test_zdf_search_resolves_real_urls_and_skips_live(monkeypatch):
    calls = []

    def request(self, url, **kwargs):
        calls.append((url, kwargs))
        if "hbbtv" in url:
            return {
                "result": [
                    {"__typename": "Video", "link": {"internalId": "episode-100"}},
                    {
                        "__typename": "EndlessSeriesSmartCollection",
                        "link": {"internalId": "show-100"},
                    },
                    {
                        "__typename": "Video",
                        "link": {"internalId": "live-100", "live": True},
                    },
                ]
            }
        return {
            "data": {
                "r0": {
                    "title": "Episode",
                    "sharingUrl": "https://www.zdf.de/video/show/episode-100",
                    "episodeInfo": {"episodeNumber": 3},
                },
                "r1": {
                    "title": "Show",
                    "sharingUrl": "https://www.zdf.de/serien/show-100",
                },
            }
        }

    monkeypatch.setattr(ZDFClient, "request", request)
    monkeypatch.setattr(ZDFClient, "headers", lambda self: {})
    c = ZDFClient()
    try:
        rows = c.search("a & b")
        assert len(rows) == 2
        assert all(r["media_type"] == "series" for r in rows)
        assert calls[0][1]["params"]["t"] == "a & b"
        assert "live-100" not in calls[1][1]["json"]["query"]
    finally:
        c.close()


@pytest.mark.parametrize("site,cls", [("zdf", ZDFClient), ("dmax", DMAXClient)])
def test_text_search_routes_and_closes_client(monkeypatch, site, cls):
    closed = []
    monkeypatch.setattr(cls, "search", lambda self, q: [{"title": q}])
    monkeypatch.setattr(cls, "close", lambda self: closed.append(True))
    assert sitesearch.search(site, "a title") == [{"title": "a title"}]
    assert closed == [True]


def test_native_hls_strict_audio_rejects_default_substitution(monkeypatch, tmp_path):
    from h0melab.models.common import hls

    text = (
        '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",LANGUAGE="en",URI="en.m3u8"\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=1000,AUDIO="a"\nvideo.m3u8\n'
    )
    monkeypatch.setattr(hls, "_fetch_text", lambda *a: text)
    with pytest.raises(hls.HLSUnsupported, match="audio language"):
        hls.download_hls_parallel(
            "https://cdn.example/master.m3u8",
            tmp_path / "out",
            preferred_audio_lang="deu",
            strict_audio=True,
            concurrency=2,
        )


def test_dmax_protected_stream_is_not_exposed(monkeypatch, catalogue):
    monkeypatch.setattr(DMAXClient, "headers", lambda self: {})

    def request(self, url, **kwargs):
        if "content/videos/" in url:
            return {"data": {"id": "1", "attributes": {"name": "Video"}}}
        return {
            "data": {
                "attributes": {
                    "streaming": [
                        {
                            "type": "hls",
                            "url": "https://cdn.example/protected.m3u8",
                            "protection": {"drmEnabled": True},
                        },
                    ]
                }
            }
        }

    monkeypatch.setattr(DMAXClient, "request", request)
    c = DMAXClient()
    try:
        with pytest.raises(BroadcasterError, match="no supported"):
            c.extract(SHOW + "/season-1-episode-1")
    finally:
        c.close()
