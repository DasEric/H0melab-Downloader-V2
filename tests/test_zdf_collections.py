"""Collection URLs must expand completely, without guessing episode numbers."""

import pytest

from h0melab.models.broadcasters.client import BroadcasterError
from h0melab.models.broadcasters.collection import (
    ZDFCollection,
    ZDFCollectionClient,
    is_zdf_collection,
)
from h0melab.models.broadcasters.episode import ZDFEpisode
from h0melab.providers import resolve_provider

SHOW = "https://www.zdf.de/magazine/test-show-104"
VIDEO = "https://www.zdf.de/video/magazine/test-show-104/episode-100"


def episode(number, url=None):
    return {
        "sharingUrl": url or VIDEO.replace("100", str(number)),
        "title": f"Episode {number}",
        "episodeInfo": {"seasonNumber": 2026, "episodeNumber": number},
    }


def page(rows, *, number=2026, more=False, cursor=None):
    return {
        "seasons": {
            "nodes": [
                {
                    "number": number,
                    "episodes": {
                        "nodes": rows,
                        "pageInfo": {"hasNextPage": more, "endCursor": cursor},
                    },
                }
            ]
        }
    }


@pytest.fixture
def collection_api(monkeypatch):
    data = {
        "title": "Test Show",
        "infoText": "Description",
        "seasons": {
            "seasons": [
                {"number": 2026, "title": "2026", "countEpisodes": 3},
                {"number": 2025, "title": "2025", "countEpisodes": 1},
            ]
        },
    }
    calls = []
    monkeypatch.setattr(ZDFCollectionClient, "resolve_url", lambda self, url: url)
    monkeypatch.setattr(ZDFCollectionClient, "collection", lambda self, canonical: data)

    def fetch(self, canonical, index, cursor=None):
        calls.append((index, cursor))
        if index == 1:
            return page([episode(4)], number=2025)
        if cursor is None:
            return page([episode(1), episode(2)], more=True, cursor="next")
        return page([episode(2), episode(3)])

    monkeypatch.setattr(ZDFCollectionClient, "season_page", fetch)
    return data, calls


def test_distinct_collection_and_video_routes():
    assert is_zdf_collection(SHOW)
    assert not is_zdf_collection(VIDEO)
    assert not is_zdf_collection("https://www.zdf.de/old/video.html")
    assert not is_zdf_collection("https://zdf.de.evil.test/show")
    provider = resolve_provider(SHOW)
    assert isinstance(provider.series_cls(url=SHOW), ZDFCollection)
    assert isinstance(provider.series_cls(url=VIDEO), ZDFEpisode)
    assert not provider.series_pattern.fullmatch(VIDEO)


def test_season_filter_preserves_original_index(collection_api):
    _, calls = collection_api
    collection = ZDFCollection(SHOW + "?staffel=2025")
    assert len(collection.season_rows()) == 1
    assert collection.season_rows()[0]["season_number"] == 2025
    assert len(collection.episode_rows()) == 1
    assert calls == [(1, None)]


def test_cursor_pagination_deduplicates_boundary_entries(collection_api):
    _, calls = collection_api
    collection = ZDFCollection(SHOW + "?staffel=2026")
    rows = collection.episode_rows()
    assert [row["episode_number"] for row in rows] == [1, 2, 3]
    assert calls == [(0, None), (0, "next")]
    assert rows[0]["available_languages"] == []


@pytest.mark.parametrize("season", ["invalid", "-1"])
def test_invalid_season_query(season):
    with pytest.raises(BroadcasterError, match="invalid requested season"):
        ZDFCollection(SHOW + "?staffel=" + season)


def test_missing_season_is_error_not_empty_success(collection_api):
    with pytest.raises(BroadcasterError, match="not available"):
        ZDFCollection(SHOW + "?staffel=999").season_rows()


@pytest.mark.parametrize("cursor", [None, "repeat"])
def test_bad_cursor_aborts_instead_of_returning_partial_list(
    collection_api, monkeypatch, cursor
):
    monkeypatch.setattr(
        ZDFCollectionClient,
        "season_page",
        lambda *args: page([episode(1)], more=True, cursor=cursor),
    )
    with pytest.raises(BroadcasterError, match="did not advance"):
        ZDFCollection(SHOW + "?staffel=2026").episode_rows()


def test_missing_pagination_metadata_is_an_error(collection_api, monkeypatch):
    result = page([episode(1)])
    del result["seasons"]["nodes"][0]["episodes"]["pageInfo"]
    monkeypatch.setattr(ZDFCollectionClient, "season_page", lambda *args: result)
    with pytest.raises(BroadcasterError, match="pagination information"):
        ZDFCollection(SHOW + "?staffel=2026").episode_rows()


def test_single_video_collection_opens_real_video_url(collection_api):
    data, _ = collection_api
    data["seasons"] = {"seasons": []}
    data["video"] = {"sharingUrl": VIDEO}
    collection = ZDFCollection(SHOW)
    assert collection.is_movie
    assert collection.season_rows()[0]["url"] == VIDEO
    assert collection.season_rows()[0]["season_number"] is None


def test_missing_number_is_not_guessed(collection_api, monkeypatch):
    row = episode(1)
    row["episodeInfo"] = {}
    monkeypatch.setattr(ZDFCollectionClient, "season_page", lambda *args: page([row]))
    result = ZDFCollection(SHOW + "?staffel=2026").episode_rows()[0]
    assert result["episode_number"] is None
    assert result["season_number"] is None


def test_collection_web_flow(client, collection_api):
    response = client.post("/api/search", json={"site": "zdf", "keyword": SHOW})
    assert response.status_code == 200
    assert response.json["results"][0]["title"] == "Test Show"
    assert (
        client.get("/api/series", query_string={"url": SHOW}).json["media_type"]
        == "series"
    )
    seasons = client.get("/api/seasons", query_string={"url": SHOW}).json["seasons"]
    assert len(seasons) == 2
    response = client.get("/api/episodes", query_string={"url": seasons[0]["url"]})
    assert response.status_code == 200
    assert len(response.json["episodes"]) == 3
    assert all(
        row["url"].startswith("https://www.zdf.de/video/")
        for row in response.json["episodes"]
    )


def test_collection_query_uses_native_graphql(monkeypatch):
    client = ZDFCollectionClient()
    calls = []
    monkeypatch.setattr(client, "headers", lambda: {"Api-Auth": "test"})

    def request(url, **kwargs):
        calls.append(kwargs)
        return {"data": {"smartCollectionByCanonical": {"title": "Show"}}}

    monkeypatch.setattr(client, "request", request)
    try:
        assert client.collection("show-100")["title"] == "Show"
        assert calls[0]["params"]["operationName"] == "GetSmartCollectionByCanonical"
    finally:
        client.close()
