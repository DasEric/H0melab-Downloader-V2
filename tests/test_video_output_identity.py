"""Equivalent legacy reservations must not block the same AniWorld episode."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from h0melab.config import GLOBAL_SESSION
from h0melab.models.h0melab_to.episode import AniworldEpisode
from h0melab.web import db, media_targets, video_library, worker

SERIES_URL = "https://aniworld.to/anime/stream/one-piece"
IDENTITY = {
    "media_type": "series",
    "title": "One Piece",
    "year": "1999-2025",
    "season": 1,
    "episode": 1,
    "end": 1,
}
COLLISION = "Two different titles map to the same filename; review this media identity"


def legacy_reservation(path, identity):
    """Seed exactly the raw JSON format persisted before this correction."""
    key = json.dumps(
        {
            field: identity.get(field)
            for field in (
                "media_type",
                "title",
                "year",
                "season",
                "episode",
                "end",
                "id",
            )
        },
        sort_keys=True,
    )
    with db.session() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS video_outputs(path TEXT PRIMARY KEY, identity TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO video_outputs VALUES (?,?)",
            (os.path.normcase(str(Path(path).absolute())), key),
        )
    return key


def reserved(path):
    with db.session() as conn:
        return conn.execute(
            "SELECT identity FROM video_outputs WHERE path=?",
            (os.path.normcase(str(Path(path).absolute())),),
        ).fetchone()[0]


@pytest.mark.parametrize(
    "old_year,new_year",
    [
        ("1999-2025", "1999-2026"),
        ("1999-2026", "1999-2027"),
        ("1999 - 2025", "1999-2026"),
        ("1999\u20132025", "1999-2026"),
        (1999, "1999"),
        (None, ""),
    ],
)
def test_equivalent_release_metadata_reuses_legacy_reservation(
    tmp_path, old_year, new_year
):
    path = tmp_path / "One Piece - S01E01.mkv"
    legacy_reservation(path, {**IDENTITY, "year": old_year})
    identity = {**IDENTITY, "year": new_year}
    video_library.reserve_output(path, identity)
    # A new DB connection sees the normalized reservation, not a process cache.
    video_library.reserve_output(path, identity)
    assert json.loads(reserved(path))["year"] == (
        "1999" if old_year is not None else ""
    )
    assert not path.exists()


@pytest.mark.parametrize("include_end", [False, True])
def test_implicit_and_explicit_single_episode_end_are_equivalent(tmp_path, include_end):
    path = video_library.output_path(tmp_path, IDENTITY)
    old = dict(IDENTITY)
    if not include_end:
        old.pop("end")
    legacy_reservation(path, old)
    current = dict(IDENTITY)
    if include_end:
        current.pop("end")
    video_library.reserve_output(path, current)
    assert json.loads(reserved(path))["end"] == 1


@pytest.mark.parametrize("media_type", ["movie", "series"])
def test_missing_and_empty_year_are_equivalent(tmp_path, media_type):
    path = tmp_path / "video.mkv"
    old = {**IDENTITY, "media_type": media_type}
    old.pop("year")
    legacy_reservation(path, old)
    video_library.reserve_output(path, {**old, "year": ""})


def test_legacy_movie_episode_placeholders_do_not_change_its_identity(tmp_path):
    path = tmp_path / "movie.mkv"
    legacy_reservation(path, {**IDENTITY, "media_type": "movie"})
    movie = {"media_type": "movie", "title": "One Piece", "year": "1999-2025"}
    video_library.reserve_output(path, movie)
    assert "season" not in json.loads(reserved(path))
    assert "episode" not in json.loads(reserved(path))


def test_upgrading_one_reservation_does_not_change_other_rows(tmp_path):
    first, second = tmp_path / "first.mkv", tmp_path / "second.mkv"
    legacy_reservation(first, IDENTITY)
    untouched = legacy_reservation(second, {**IDENTITY, "episode": 2, "end": 2})
    video_library.reserve_output(first, {**IDENTITY, "year": "1999-2026"})
    assert json.loads(reserved(first))["year"] == "1999"
    assert reserved(second) == untouched


@pytest.mark.parametrize(
    "changed",
    [
        {"media_type": "movie"},
        {"title": "Another Show"},
        {"year": "1998-2026"},
        {"year": ""},
        {"year": "unknown"},
        {"year": "2026-1999"},
        {"season": 2},
        {"episode": 2},
        {"end": 2},
        {"id": "another-source-scoped-show"},
    ],
)
def test_real_identity_changes_preserve_reservation_and_file(tmp_path, changed):
    path = tmp_path / "video.mkv"
    path.write_bytes(b"existing video")
    old = legacy_reservation(path, IDENTITY)
    with pytest.raises(ValueError, match="Two different titles"):
        video_library.reserve_output(path, {**IDENTITY, **changed})
    assert reserved(path) == old
    assert path.read_bytes() == b"existing video"


def test_sanitized_title_collision_still_fails_after_year_normalization(tmp_path):
    first = {**IDENTITY, "title": "One: Piece"}
    second = {**IDENTITY, "title": "One? Piece", "year": "1999-2026"}
    path = video_library.output_path(tmp_path, first)
    assert path == video_library.output_path(tmp_path, second)
    old = legacy_reservation(path, first)
    with pytest.raises(ValueError, match="Two different titles"):
        video_library.reserve_output(path, second)
    assert reserved(path) == old


@pytest.mark.parametrize("invalid", ["not json", "[]", "null"])
def test_invalid_stored_identity_is_not_overwritten(tmp_path, invalid):
    path = tmp_path / "video.mkv"
    legacy_reservation(path, IDENTITY)
    with db.session() as conn:
        conn.execute("UPDATE video_outputs SET identity=?", (invalid,))
    with pytest.raises(ValueError, match="Two different titles"):
        video_library.reserve_output(path, IDENTITY)
    assert reserved(path) == invalid


@pytest.fixture
def one_piece(monkeypatch, tmp_path):
    state = {"end_year": "2025", "downloads": []}
    numbers = (1, 1000, 1100)
    shows, movies = tmp_path / "shows", tmp_path / "movies"
    shows.mkdir()
    movies.mkdir()
    movie_id = db.add_custom_path("Movies", str(movies))
    show_id = db.add_custom_path("Shows", str(shows))
    media_targets.save(movie_id, show_id, 0)

    def fetch(url, *args, **kwargs):
        assert url.startswith(SERIES_URL), url
        title = (
            '<div class="series-title"><h1><span>One Piece</span></h1><small>'
            '<span itemprop="startDate"><a href="/animes/jahr/1999">1999</a></span>'
            f'<span itemprop="endDate"><a href="/animes/jahr/{state["end_year"]}">'
            f"{state['end_year']}</a></span></small></div>"
        )
        rows = "".join(
            '<tr itemtype="http://schema.org/Episode">'
            f'<meta itemprop="episodeNumber" content="{number}">'
            f'<a itemprop="url" href="/anime/stream/one-piece/staffel-1/episode-{number}">'
            f"<strong>Folge {number}</strong><span>Episode {number}</span></a></tr>"
            for number in numbers
        )
        player = (
            '<span class="episodeGermanTitle">Eine Folge</span>'
            '<li data-lang-key="1"><a href="/redirect/1"><h4>VOE</h4></a></li>'
        )
        return SimpleNamespace(text=title + rows + player)

    def download(episode):
        state["downloads"].append(episode.episode_number)
        episode._episode_path.write_bytes(b"synthetic One Piece episode")

    monkeypatch.setattr(GLOBAL_SESSION, "get", fetch)
    monkeypatch.setattr(AniworldEpisode, "download", download)
    return state, numbers, shows, movies


@pytest.mark.parametrize("existing_file", [False, True])
def test_one_piece_requeues_old_year_range_without_losing_existing_files(
    client, one_piece, existing_file
):
    state, numbers, shows, movies = one_piece
    urls = [f"{SERIES_URL}/staffel-1/episode-{number}" for number in numbers]
    outputs = []
    for number, url in zip(numbers, urls):
        episode = AniworldEpisode(url)
        identity = video_library.identity_for_episode(episode, url)
        assert identity["year"] == "1999-2025"
        path = video_library.output_path(shows, identity)
        path = path.with_name(path.stem + " - german-dub.mkv")
        legacy_reservation(path, identity)
        outputs.append(path)
    if existing_file:
        outputs[0].parent.mkdir(parents=True)
        outputs[0].write_bytes(b"existing One Piece episode")

    # A source refresh changes the end year, not the show's premiere or episode.
    state["end_year"] = "2026"
    response = client.get(
        "/api/episodes", query_string={"url": SERIES_URL + "/staffel-1"}
    )
    assert response.status_code == 200, response.get_json()
    assert len(response.get_json()["episodes"]) == len(numbers)
    assert all(video_library.observed(url)["year"] == "1999-2026" for url in urls)
    response = client.post(
        "/api/download",
        json={"episodes": urls, "title": "One Piece", "provider": "VOE"},
    )
    assert response.status_code == 200, response.get_json()
    item = db.get_queue_item(response.get_json()["queue_id"])
    worker._process(item)
    result = db.get_queue_item(item["id"])
    assert result["status"] == "completed", result["errors"]
    assert result["errors"] == "[]"
    expected = list(numbers[1:] if existing_file else numbers)
    assert state["downloads"] == expected
    assert all(path.is_file() for path in outputs)
    assert outputs[0].parent == shows / "One Piece/Season 01"
    assert all(json.loads(reserved(path))["year"] == "1999" for path in outputs)
    if existing_file:
        assert outputs[0].read_bytes() == b"existing One Piece episode"
    assert not list(movies.rglob("*.mkv"))

    # Restart/retry-style new models and a later end year must remain idempotent.
    state["end_year"] = "2027"
    client.get("/api/episodes", query_string={"url": SERIES_URL + "/staffel-1"})
    again = client.post("/api/download", json={"episodes": urls, "provider": "VOE"})
    assert again.status_code == 200, again.get_json()
    queue_id = again.get_json()["queue_id"]
    worker._process(db.get_queue_item(queue_id))
    repeated = db.get_queue_item(queue_id)
    assert repeated["status"] == "completed", repeated["errors"]
    assert repeated["errors"] == "[]"
    assert state["downloads"] == expected
