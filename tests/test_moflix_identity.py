"""Moflix metadata, observed identities and the real typed queue must agree."""

import json
import re
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from h0melab.models.moflix_stream import series as moflix
from h0melab.web import connector_files, db, media_targets, video_library, worker
from h0melab.web.views import api_media

TITLE_URL = "https://moflix-stream.xyz/titles/42"
MOVIE_IDENTITY = {"media_type": "movie", "title": "Sample", "year": "2026"}


@pytest.fixture
def moflix_source(monkeypatch):
    """Keep the real models/queue/paths; replace only HTTP and media transfer."""
    state = {
        "is_series": False,
        "title": "Sample",
        "seasons": {1: [1, 2], 2: [1, 2]},
        "calls": [],
        "downloads": [],
    }

    def fetch(url, *args, **kwargs):
        state["calls"].append(url)
        path = urlsplit(url).path
        match = re.fullmatch(
            r"/api/v1/titles/(42|43)(?:/seasons/(\d+)(?:/episodes/(\d+))?)?",
            path,
        )
        payload = None
        if match:
            title_id, season, episode = match.groups()
            if episode:
                payload = {"episode": {"videos": []}}
            elif season:
                payload = {
                    "episodes": {
                        "data": [
                            {"episode_number": number, "name": f"Episode {number}"}
                            for number in state["seasons"][int(season)]
                        ]
                    }
                }
            else:
                payload = {
                    "title": {
                        "name": state["title"] if title_id == "42" else "Earlier",
                        "year": 2026,
                        "is_series": state["is_series"] if title_id == "42" else False,
                        "videos": [],
                    },
                    "seasons": {
                        "data": [
                            {"number": number, "episodes_count": len(episodes)}
                            for number, episodes in state["seasons"].items()
                        ]
                    }
                    if state["is_series"] and title_id == "42"
                    else {},
                }
        else:
            assert path.startswith("/titles/"), url
        return SimpleNamespace(
            status_code=200,
            headers={},
            cookies={},
            text='window.bootstrapData = {"csrf_token":"test"};',
            json=lambda: payload,
            raise_for_status=lambda: None,
        )

    def download(episode):
        state["downloads"].append(episode.url)
        episode._episode_path.parent.mkdir(parents=True, exist_ok=True)
        episode._episode_path.write_bytes(b"synthetic Moflix media")

    monkeypatch.setattr(moflix, "_fetch_moflix", fetch)
    monkeypatch.setattr(moflix.MoflixEpisode, "download", download)
    return state


@pytest.fixture
def typed_targets(tmp_path):
    movies, shows = tmp_path / "movies", tmp_path / "shows"
    movies.mkdir()
    shows.mkdir()
    movie_id = db.add_custom_path("Movies", str(movies))
    show_id = db.add_custom_path("Shows", str(shows))
    media_targets.save(movie_id, show_id, 0)
    return movie_id, show_id, movies, shows


def submit(client, url=TITLE_URL, **options):
    response = client.post(
        "/api/download",
        json={"episodes": [url], "provider": "MoflixClick", **options},
    )
    assert response.status_code == 200, response.get_json()
    return db.get_queue_item(response.get_json()["queue_id"])


def movie_output(typed_targets):
    return typed_targets[2] / "Sample (2026)/Sample (2026) - german-dub.mkv"


def test_movie_identity_uses_metadata_not_placeholder_numbers(moflix_source):
    movie = moflix.MoflixEpisode(TITLE_URL)
    listed = movie.seasons[0].episodes[0]
    # Movie wrappers still have a UI episode 1; this is not a series identity.
    assert listed.season_number == listed.episode_number == 1
    assert movie.is_movie is True
    assert video_library.identity_for_episode(movie, TITLE_URL) == MOVIE_IDENTITY
    assert video_library.identity_for_episode(listed, listed.url) == MOVIE_IDENTITY


def test_movie_metadata_takes_precedence_over_episode_like_title(moflix_source):
    moflix_source["title"] = "Sample S01E02"
    movie = moflix.MoflixEpisode(TITLE_URL)
    assert video_library.identity_for_episode(movie, TITLE_URL) == {
        **MOVIE_IDENTITY,
        "title": "Sample S01E02",
    }


@pytest.mark.parametrize(
    "suffix,season,episode",
    [
        ("/season/1/episodes/2", 1, 2),
        ("/season/2/episodes/1", 2, 1),
        ("?season=2&episode=2", 2, 2),
        ("/season/0/episodes/1", 0, 1),
    ],
)
def test_series_metadata_preserves_year_and_numbering(
    moflix_source, suffix, season, episode
):
    moflix_source["is_series"] = True
    url = TITLE_URL + suffix
    model = moflix.MoflixEpisode(url)
    assert video_library.identity_for_episode(model, url) == {
        "media_type": "series",
        "title": "Sample",
        "year": "2026",
        "season": season,
        "episode": episode,
        "end": episode,
    }
    assert model.is_movie is False


@pytest.mark.parametrize("is_series", [False, True])
def test_listing_and_fresh_model_store_the_same_identity_without_player_probes(
    client, moflix_source, is_series
):
    moflix_source["is_series"] = is_series
    detail = client.get("/api/series", query_string={"url": TITLE_URL}).get_json()
    assert detail["is_movie"] is (not is_series)
    response = client.get("/api/episodes", query_string={"url": TITLE_URL})
    assert response.status_code == 200, response.get_json()
    for row in response.get_json()["episodes"]:
        fresh = moflix.MoflixEpisode(row["url"])
        assert video_library.observed(row["url"]) == video_library.identity_for_episode(
            fresh, row["url"]
        )
    assert not any(
        "/api/v1/" in url and "/episodes/" in url for url in moflix_source["calls"]
    )


@pytest.mark.parametrize("listed", [False, True])
@pytest.mark.parametrize("behind_other", [False, True])
def test_movie_queue_keeps_film_identity_and_destination(
    client, moflix_source, typed_targets, listed, behind_other
):
    if behind_other:
        earlier = submit(client, "https://moflix-stream.xyz/titles/43")
    if listed:
        response = client.get("/api/episodes", query_string={"url": TITLE_URL})
        assert response.status_code == 200
    item = submit(client)
    entry = json.loads(item["episodes"])[0]
    assert entry["_media_identity"] == MOVIE_IDENTITY
    assert entry["custom_path_id"] == typed_targets[0]
    if behind_other:
        first = worker._claim_next()
        assert first["id"] == earlier["id"]
        worker._process(first)
        assert db.get_queue_item(first["id"])["status"] == "completed"
    claimed = worker._claim_next()
    assert claimed["id"] == item["id"]
    worker._process(claimed)
    result = db.get_queue_item(item["id"])
    assert result["status"] == "completed", result["errors"]
    assert result["errors"] == "[]"
    assert movie_output(typed_targets).read_bytes() == b"synthetic Moflix media"
    assert not list(typed_targets[3].rglob("*.mkv"))


@pytest.mark.parametrize("listed", [False, True])
def test_series_queue_preserves_non_first_season_and_episode(
    client, moflix_source, typed_targets, listed
):
    moflix_source["is_series"] = True
    url = TITLE_URL + "/season/2/episodes/2"
    if listed:
        response = client.get(
            "/api/episodes", query_string={"url": TITLE_URL + "?season=2"}
        )
        assert response.status_code == 200
    item = submit(client, url)
    worker._process(item)
    result = db.get_queue_item(item["id"])
    assert result["status"] == "completed", result["errors"]
    assert (
        typed_targets[3]
        / "Sample (2026)/Season 02/Sample (2026) - S02E02 - german-dub.mkv"
    ).is_file()
    assert not list(typed_targets[2].rglob("*.mkv"))


def test_direct_submission_revalidates_cached_moflix_identity(
    client, moflix_source, typed_targets
):
    video_library.remember(
        TITLE_URL,
        {**MOVIE_IDENTITY, "media_type": "series", "season": 1, "episode": 1, "end": 1},
    )
    item = submit(client)
    assert json.loads(item["episodes"])[0]["_media_identity"] == MOVIE_IDENTITY
    assert video_library.observed(TITLE_URL) == MOVIE_IDENTITY
    worker._process(item)
    assert db.get_queue_item(item["id"])["status"] == "completed"
    assert movie_output(typed_targets).is_file()


def test_refresh_failure_does_not_queue_a_stale_moflix_identity(
    client, moflix_source, typed_targets, monkeypatch
):
    video_library.remember(TITLE_URL, MOVIE_IDENTITY)

    def failed_fetch(*args, **kwargs):
        raise moflix.MoflixResponseError("Moflix request timed out.")

    monkeypatch.setattr(moflix, "_fetch_moflix", failed_fetch)
    response = client.post("/api/download", json={"episodes": [TITLE_URL]})
    assert response.status_code == 409
    assert response.get_json()["error"] == "Moflix request timed out."
    assert db.get_next_queued() is None


def test_inspect_preserves_other_sources_cached_identity(monkeypatch):
    url = "https://filmpalast.to/stream/sample"
    video_library.remember(url, MOVIE_IDENTITY)

    def unexpected_model(**kwargs):
        pytest.fail("A cached non-Moflix title should not be fetched again")

    monkeypatch.setattr(
        api_media,
        "resolve_provider",
        lambda url: SimpleNamespace(name="FilmPalast", episode_cls=unexpected_model),
    )
    assert video_library.inspect(url) == MOVIE_IDENTITY


@pytest.mark.parametrize("payload_version", [1, 2, 3])
def test_retry_accepts_legacy_movie_placeholder_numbers(
    client, moflix_source, typed_targets, payload_version
):
    legacy = {**MOVIE_IDENTITY, "season": 1, "episode": 1, "end": 1}
    video_library.remember(TITLE_URL, legacy)
    entry = (
        {
            "url": TITLE_URL,
            "_media_identity": legacy,
            "custom_path_id": typed_targets[0],
            "target_root": str(typed_targets[2]),
            "_media_revision": 1,
        }
        if payload_version == 3
        else TITLE_URL
    )
    queue_id = db.add_to_queue(
        "Sample",
        TITLE_URL,
        [entry],
        "German Dub",
        "MoflixClick",
        custom_path_id=typed_targets[0],
        payload_version=payload_version,
    )
    db.set_queue_status(queue_id, "failed")
    assert client.post(f"/api/queue/{queue_id}/retry").status_code == 200
    worker._process(db.get_queue_item(queue_id))
    result = db.get_queue_item(queue_id)
    assert result["status"] == "completed", result["errors"]
    assert movie_output(typed_targets).is_file()
    assert not list(typed_targets[3].rglob("*.mkv"))

    # A legacy retry and a fresh submission must reserve the same film identity.
    again = submit(client)
    worker._process(again)
    repeated = db.get_queue_item(again["id"])
    assert repeated["status"] == "completed", repeated["errors"]
    assert moflix_source["downloads"] == [TITLE_URL]


def test_wrongly_stored_series_is_not_silently_rerouted(moflix_source, typed_targets):
    entry = {
        "url": TITLE_URL,
        "_media_identity": {
            **MOVIE_IDENTITY,
            "media_type": "series",
            "season": 1,
            "episode": 1,
        },
        "custom_path_id": typed_targets[1],
        "target_root": str(typed_targets[3]),
        "_media_revision": 1,
    }
    queue_id = db.add_to_queue(
        "Sample",
        TITLE_URL,
        [entry],
        "German Dub",
        "MoflixClick",
        payload_version=3,
    )
    worker._process(db.get_queue_item(queue_id))
    result = db.get_queue_item(queue_id)
    assert result["status"] == "failed"
    assert json.loads(result["errors"])[0]["error"] == (
        "Source identity changed since this title was queued"
    )
    assert moflix_source["downloads"] == []
    assert not list(typed_targets[2].rglob("*.mkv"))
    assert not list(typed_targets[3].rglob("*.mkv"))


@pytest.mark.parametrize("initial_series", [False, True])
def test_real_type_changes_stop_before_writing(
    client, moflix_source, typed_targets, initial_series
):
    moflix_source["is_series"] = initial_series
    client.get("/api/episodes", query_string={"url": TITLE_URL})
    url = TITLE_URL + "/season/1/episodes/1" if initial_series else TITLE_URL
    item = submit(client, url)
    moflix_source["is_series"] = not initial_series
    worker._process(item)
    result = db.get_queue_item(item["id"])
    assert result["status"] == "failed"
    assert "Source identity changed" in result["errors"]
    assert moflix_source["downloads"] == []


@pytest.mark.parametrize("field", ["season", "episode"])
def test_series_number_conflicts_still_stop_before_writing(
    client, moflix_source, typed_targets, field
):
    moflix_source["is_series"] = True
    item = submit(client, TITLE_URL + "/season/1/episodes/1")
    entries = json.loads(item["episodes"])
    entries[0]["_media_identity"][field] = 2
    with db.session() as conn:
        conn.execute(
            "UPDATE download_queue SET episodes=? WHERE id=?",
            (json.dumps(entries), item["id"]),
        )
    worker._process(db.get_queue_item(item["id"]))
    result = db.get_queue_item(item["id"])
    assert result["status"] == "failed"
    assert "Source identity changed" in result["errors"]
    assert moflix_source["downloads"] == []


def test_movie_still_rechecks_destination_revision(
    client, moflix_source, typed_targets
):
    client.get("/api/episodes", query_string={"url": TITLE_URL})
    item = submit(client)
    media_targets.save(typed_targets[0], typed_targets[1], 1)
    worker._process(item)
    result = db.get_queue_item(item["id"])
    assert result["status"] == "failed"
    assert "Media destinations changed" in result["errors"]
    assert moflix_source["downloads"] == []


def test_movie_does_not_overwrite_an_existing_file(
    client, moflix_source, typed_targets
):
    output = movie_output(typed_targets)
    output.parent.mkdir(parents=True)
    output.write_bytes(b"existing manual media")
    item = submit(client)
    worker._process(item)
    result = db.get_queue_item(item["id"])
    assert result["status"] == "completed", result["errors"]
    assert output.read_bytes() == b"existing manual media"
    assert moflix_source["downloads"] == []


@pytest.mark.parametrize("listed", [False, True])
def test_connector_movie_receipt_retains_film_destination(
    client, moflix_source, typed_targets, monkeypatch, listed
):
    monkeypatch.setattr(
        connector_files,
        "media_streams",
        lambda path: [{"codec_type": "audio", "tags": {"language": "deu"}}],
    )
    if listed:
        assert (
            client.get("/api/episodes", query_string={"url": TITLE_URL}).status_code
            == 200
        )
    response = client.post(
        "/api/download",
        json={
            "connector_protocol": 1,
            "request_key": "moflix-movie-receipt",
            "episodes": [
                {
                    "url": TITLE_URL,
                    "language": "German Dub",
                    "provider": "MoflixClick",
                    "media_type": "movie",
                    "custom_path_id": typed_targets[0],
                    "target_root": str(typed_targets[2]),
                }
            ],
        },
    )
    assert response.status_code == 200, response.get_json()
    queue_id = response.get_json()["queue_id"]
    worker._process(db.get_queue_item(queue_id))
    result = db.get_queue_item(queue_id)
    assert result["status"] == "completed", result["errors"]
    receipts = connector_files.query(["moflix-movie-receipt"])
    assert len(receipts) == 1
    assert receipts[0]["media_type"] == "movie"
    assert receipts[0]["custom_path_id"] == typed_targets[0]
    assert receipts[0]["state"] == "owned"
    assert receipts[0]["relative_path"] == (
        "Sample (2026)/Sample (2026) - german-dub.mkv"
    )
    assert not list(typed_targets[3].rglob("*.mkv"))


def test_download_without_typed_targets_keeps_legacy_movie_path(
    client, moflix_source, downloads
):
    assert media_targets.read()["enabled"] is False
    item = submit(client)
    assert item["payload_version"] == 1
    worker._process(item)
    result = db.get_queue_item(item["id"])
    assert result["status"] == "completed", result["errors"]
    assert list(downloads.rglob("*.mkv"))
    assert not any(path.name.startswith("Season") for path in downloads.rglob("*"))
