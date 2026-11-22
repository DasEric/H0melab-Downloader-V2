from copy import deepcopy

import pytest

from h0melab.web import tmdb


@pytest.fixture
def responses(monkeypatch):
    data = {
        "movie/157336": {
            "id": 157336,
            "title": "Interstellar",
            "original_title": "Interstellar",
            "release_date": "2014-11-05",
        },
        "movie/157336/external_ids": {"id": 157336, "imdb_id": "tt0816692"},
        "movie/157336/alternative_titles": {
            "id": 157336,
            "titles": [{"title": "Interstellar Alternative"}],
        },
        "find/tt0816692": {"movie_results": [{"id": 157336}], "tv_results": []},
    }
    monkeypatch.setattr(tmdb, "_get", lambda path, **kwargs: deepcopy(data[path]))
    return data


@pytest.mark.parametrize(
    "ids",
    [
        {"tmdb_id": 157336},
        {"imdb_id": "tt0816692"},
        {"tmdb_id": 157336, "imdb_id": "tt0816692"},
    ],
)
def test_verified_identity(responses, ids):
    result = tmdb.identity_details("movie", **ids)
    assert result["tmdb_id"] == 157336 and result["imdb_id"] == "tt0816692"
    assert result["release_year"] == 2014
    assert result["alternative_titles"] == ["Interstellar Alternative"]


@pytest.mark.parametrize(
    "ids",
    [
        {"tmdb_id": True},
        {"tmdb_id": -1},
        {"imdb_id": "bad"},
        {"tmdb_id": "157336"},
        {},
    ],
)
def test_invalid_ids(responses, ids):
    with pytest.raises(tmdb.TMDBError):
        tmdb.identity_details("movie", **ids)


def test_ids_never_linked_without_proof(responses):
    with pytest.raises(tmdb.TMDBError, match="does not match"):
        tmdb.identity_details("movie", tmdb_id=157336, imdb_id="tt0773262")


def test_wrong_type_is_not_resolved(responses):
    with pytest.raises(tmdb.TMDBError, match="not uniquely resolved"):
        tmdb.identity_details("tv", imdb_id="tt0816692")


def test_ambiguous_imdb_result(responses):
    responses["find/tt0816692"]["movie_results"].append({"id": 1})
    with pytest.raises(tmdb.TMDBError):
        tmdb.identity_details("movie", imdb_id="tt0816692")


@pytest.mark.parametrize(
    "path",
    ["movie/157336", "movie/157336/external_ids", "movie/157336/alternative_titles"],
)
def test_wrong_response_identity(responses, path):
    responses[path]["id"] = 1
    with pytest.raises(tmdb.TMDBError):
        tmdb.identity_details("movie", tmdb_id=157336)


def test_missing_imdb_can_remain_missing(responses):
    responses["movie/157336/external_ids"]["imdb_id"] = None
    assert tmdb.identity_details("movie", tmdb_id=157336)["imdb_id"] is None
    with pytest.raises(tmdb.TMDBError):
        tmdb.identity_details("movie", tmdb_id=157336, imdb_id="tt0816692")


@pytest.mark.parametrize(
    "change",
    [
        {"season_number": 2},
        {"episode_number": True},
        {"show_id": 2},
        {"episode_number": 0},
    ],
)
def test_invalid_episode_response(monkeypatch, change):
    row = {"season_number": 1, "episode_number": 1, "show_id": 1405, "name": "Dexter"}
    row.update(change)
    monkeypatch.setattr(
        tmdb, "_get", lambda *a, **k: {"season_number": 1, "episodes": [row]}
    )
    with pytest.raises(tmdb.TMDBError):
        tmdb.season_details(1405, 1)


def test_duplicate_episode_numbers(monkeypatch):
    row = {"season_number": 1, "episode_number": 1, "show_id": 1405}
    monkeypatch.setattr(
        tmdb, "_get", lambda *a, **k: {"season_number": 1, "episodes": [row, dict(row)]}
    )
    with pytest.raises(tmdb.TMDBError):
        tmdb.season_details(1405, 1)


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_temporary_api_failures_are_distinguishable(monkeypatch, status):
    from types import SimpleNamespace

    monkeypatch.setenv(tmdb.TMDB_KEY, "key")
    calls = []
    monkeypatch.setattr(tmdb.time, "sleep", lambda _: None)

    def get(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=status)

    monkeypatch.setattr(tmdb.requests, "get", get)
    with pytest.raises(tmdb.TMDBTransientError):
        tmdb._get("movie/42")
    assert len(calls) == 3
    assert all(call["timeout"] == 15 and not call["allow_redirects"] for call in calls)


def test_bad_credentials_are_not_automatically_retryable(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv(tmdb.TMDB_KEY, "key")
    monkeypatch.setattr(
        tmdb.requests, "get", lambda *a, **k: SimpleNamespace(status_code=401)
    )
    with pytest.raises(tmdb.TMDBError) as caught:
        tmdb._get("movie/42")
    assert not isinstance(caught.value, tmdb.TMDBTransientError)


@pytest.fixture
def tv_responses(monkeypatch):
    data = {
        "tv/1405": {
            "id": 1405,
            "name": "Dexter",
            "original_name": "Dexter",
            "first_air_date": "2006-10-01",
            "seasons": [{"season_number": 0}, {"season_number": 1}],
        },
        "tv/1405/external_ids": {"id": 1405, "imdb_id": "tt0773262"},
        "tv/1405/alternative_titles": {
            "id": 1405,
            "results": [{"title": "Dexter Alternative"}],
        },
        "find/tt0773262": {
            "movie_results": [{"id": 999}],
            "tv_results": [{"id": 1405}],
        },
    }
    monkeypatch.setattr(tmdb, "_get", lambda path, **kwargs: deepcopy(data[path]))
    return data


@pytest.mark.parametrize(
    "ids",
    [
        {"tmdb_id": 1405},
        {"imdb_id": "tt0773262"},
        {"tmdb_id": 1405, "imdb_id": "tt0773262"},
    ],
)
def test_tv_identity_uses_typed_endpoints_and_external_ids(tv_responses, ids):
    result = tmdb.identity_details("tv", **ids)
    assert result["media_type"] == "tv"
    assert result["tmdb_id"] == 1405
    assert result["imdb_id"] == "tt0773262"
    assert result["title"] == "Dexter" and result["original_title"] == "Dexter"
    assert result["release_year"] == 2006
    assert result["alternative_titles"] == ["Dexter Alternative"]
    assert [row["season_number"] for row in result["seasons"]] == [0, 1]


@pytest.mark.parametrize(
    "seasons",
    [
        [{"season_number": 1}, {"season_number": 1}],
        [{"season_number": True}],
        [{"season_number": -1}],
    ],
)
def test_tv_invalid_or_duplicate_seasons_are_rejected(tv_responses, seasons):
    tv_responses["tv/1405"]["seasons"] = seasons
    with pytest.raises(tmdb.TMDBError):
        tmdb.identity_details("tv", tmdb_id=1405)


def test_tv_id_contradiction_is_not_overridden_by_title(tv_responses):
    tv_responses["tv/1405/external_ids"]["imdb_id"] = "tt9999999"
    with pytest.raises(tmdb.TMDBError, match="does not match"):
        tmdb.identity_details("tv", tmdb_id=1405, imdb_id="tt0773262")


def test_tv_imdb_ambiguity_is_not_resolved_using_movie_result(tv_responses):
    tv_responses["find/tt0773262"]["tv_results"].append({"id": 42})
    with pytest.raises(tmdb.TMDBError, match="not uniquely"):
        tmdb.identity_details("tv", imdb_id="tt0773262")
