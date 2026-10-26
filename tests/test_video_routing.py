"""Real HTTP contracts, persistent episode families and actual isolated files."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from h0melab import search
from h0melab.web import (
    connector_files,
    db,
    media_targets,
    sitesearch,
    video_library,
    worker,
)
from h0melab.web.search_contract import media_type
from h0melab.web.views import api_media


def card(title, slug=None, site="filmpalast"):
    slug = slug or title.lower().replace(" ", "-")
    base = (
        "https://filmpalast.to/stream/"
        if site == "filmpalast"
        else "https://filmo.to/movies/"
    )
    return {"title": title, "url": base + slug, "poster": ""}


@pytest.fixture
def flat(monkeypatch):
    rows = [card("SiloS01E01"), card("Silo S01E02"), card("Silo.S02E01")]
    monkeypatch.setitem(sitesearch.SITE_SEARCH, "filmpalast", lambda query: rows)
    monkeypatch.setattr(
        search,
        "query_filmpalast",
        lambda *args, **kwargs: search.CompleteSearchResults(rows, complete=True),
    )

    class Episode:
        def __init__(
            self, url, selected_path=None, selected_language="German Dub", **kwargs
        ):
            self.url = url
            self.title = next(
                (row["title"] for row in rows if row["url"] == url), "A Film"
            )
            self.selected_path = selected_path
            self.selected_language = selected_language
            self.is_movie = video_library.parse_title(self.title) is None
            self.release_year = "2023"
            self.provider_data = {"German Dub": ["VOE"], "English Dub": ["VOE"]}

        @property
        def _episode_path(self):
            return connector_files.episode_path(
                self, Path(self.selected_path) / (self.title + ".mkv")
            )

        def download(self):
            self._episode_path.parent.mkdir(parents=True, exist_ok=True)
            self._episode_path.write_bytes(b"isolated synthetic video")

    provider = SimpleNamespace(
        name="FilmPalast", episode_cls=Episode, series_cls=Episode
    )
    monkeypatch.setattr(api_media, "resolve_provider", lambda url: provider)
    monkeypatch.setattr(worker, "resolve_provider", lambda url: provider)
    monkeypatch.setattr(api_media.media, "provider_map", lambda data, **kwargs: data)
    return rows


@pytest.fixture
def targets(downloads, tmp_path):
    movies = tmp_path / "movies"
    movies.mkdir()
    movie_id = db.add_custom_path("Movies", str(movies), "filmpalast")
    show_id = db.add_custom_path("Shows", str(downloads))
    media_targets.save(movie_id, show_id, 0)
    return movie_id, show_id, movies, downloads


@pytest.mark.parametrize(
    "title,season,episode",
    [
        ("SiloS01E01", 1, 1),
        ("Silo S1 E2", 1, 2),
        ("Silo.2x03", 2, 3),
        ("Silo S00E01", 0, 1),
        ("Silo S01E101", 1, 101),
    ],
)
def test_episode_patterns(title, season, episode):
    identity = video_library.parse_title(title)
    assert identity["title"] == "Silo"
    assert (identity["season"], identity["episode"]) == (season, episode)
    assert media_type("filmpalast", {"title": title}) == "series"


@pytest.mark.parametrize(
    "title", ["1917", "Silo", "Ocean's Eleven", "Episode", "Film 2023", "S01E01"]
)
def test_plain_movie_titles_do_not_invent_episodes(title):
    assert video_library.parse_title(title) is None


def test_one_source_scoped_card_and_stable_real_anchor(client, flat):
    response = client.post(
        "/api/search", json={"keyword": "silo", "site": "filmpalast"}
    )
    assert response.status_code == 200
    result = response.get_json()["results"]
    assert len(result) == 1 and result[0]["title"] == "Silo"
    anchor = result[0]["url"]
    assert anchor == flat[0]["url"] and result[0]["media_type"] == "series"
    flat.reverse()
    again = client.post(
        "/api/search", json={"keyword": "silo", "site": "filmpalast"}
    ).get_json()["results"][0]
    assert (again["url"], again["video_id"]) == (anchor, result[0]["video_id"])
    assert video_library.by_url(anchor)["title"] == "Silo"
    assert (
        len(video_library.group_results("filmo", [card("SiloS01E01", site="filmo")]))
        == 1
    )
    assert (
        video_library.by_url(card("SiloS01E01", site="filmo")["url"])["id"]
        != again["video_id"]
    )


def test_remakes_films_and_variants_stay_distinct(client, flat):
    flat.extend(
        [
            card("Silo (2020) S01E01"),
            card("Silo", "film-silo"),
            card("SiloS01E01", "another-hoster"),
        ]
    )
    results = client.post(
        "/api/search", json={"keyword": "silo", "site": "filmpalast"}
    ).get_json()["results"]
    assert len(results) == 3
    show = next(
        row for row in results if row.get("grouped_series") and row.get("year") == ""
    )
    seasons = client.get("/api/seasons", query_string={"url": show["url"]}).get_json()[
        "seasons"
    ]
    assert [row["episode_count"] for row in seasons] == [2, 1]


def test_group_detail_and_direct_episode_links_resolve_the_full_show(client, flat):
    # No preceding search is required for a direct episode link.
    url = flat[0]["url"]
    detail = client.get("/api/series", query_string={"url": url}).get_json()
    assert detail["title"] == "Silo" and detail["is_movie"] is False
    seasons = client.get("/api/seasons", query_string={"url": url}).get_json()[
        "seasons"
    ]
    assert [row["season_number"] for row in seasons] == [1, 2]
    episodes = client.get(
        "/api/episodes", query_string={"url": seasons[1]["url"]}
    ).get_json()["episodes"]
    assert (
        len(episodes) == 1
        and episodes[0]["season_number"] == 2
        and episodes[0]["episode_number"] == 1
    )


def test_partial_source_lists_are_never_reported_complete(client, flat, monkeypatch):
    video_library.group_results("filmpalast", flat)
    monkeypatch.setattr(
        search,
        "query_filmpalast",
        lambda *args, **kwargs: search.CompleteSearchResults(flat[:1], complete=False),
    )
    response = client.get("/api/seasons", query_string={"url": flat[0]["url"]})
    assert response.status_code != 200
    assert "incomplete" in response.get_json()["error"]


def test_removed_variants_are_not_added_back_on_refresh(client, flat, monkeypatch):
    video_library.group_results("filmpalast", flat)
    group = video_library.resolve_group(flat[0]["url"])
    assert len(video_library.active_members(group)) == 3
    with db.session() as conn:
        conn.execute("UPDATE video_titles SET refreshed=0")
    flat.pop()
    group = video_library.resolve_group(flat[0]["url"])
    assert len(video_library.active_members(group)) == 2


def test_native_queue_routes_and_writes_real_series_structure(client, flat, targets):
    _movie_id, show_id, movies, shows = targets
    client.post("/api/search", json={"keyword": "silo", "site": "filmpalast"})
    client.get("/api/seasons", query_string={"url": flat[0]["url"]})
    response = client.post(
        "/api/download",
        json={
            "episodes": [row["url"] for row in flat],
            "title": "Silo",
            "series_url": flat[0]["url"],
            "language": "German Dub",
            "provider": "VOE",
        },
    )
    assert response.status_code == 200, response.get_json()
    item = db.get_queue_item(response.get_json()["queue_id"])
    assert item["payload_version"] == 3
    assert {row["custom_path_id"] for row in json.loads(item["episodes"])} == {show_id}
    worker._process(item)
    assert db.get_queue_item(item["id"])["status"] == "completed"
    assert (shows / "Silo/Season 01/Silo - S01E01 - german-dub.mkv").is_file()
    assert (shows / "Silo/Season 02/Silo - S02E01 - german-dub.mkv").is_file()
    assert not list(movies.rglob("*.mkv"))
    film = client.post(
        "/api/download",
        json={
            "episodes": ["https://filmpalast.to/stream/film"],
            "language": "German Dub",
            "provider": "VOE",
        },
    )
    assert film.status_code == 200
    worker._process(db.get_queue_item(film.get_json()["queue_id"]))
    assert (movies / "A Film (2023)/A Film (2023) - german-dub.mkv").is_file()


def test_wrong_manual_target_and_forged_type_are_rejected(client, flat, targets):
    movie_id, _, _, _ = targets
    video_library.group_results("filmpalast", flat)
    response = client.post(
        "/api/download", json={"episodes": [flat[0]["url"]], "custom_path_id": movie_id}
    )
    assert response.status_code == 409
    response = client.post(
        "/api/download",
        json={
            "episodes": [
                {"url": flat[0]["url"], "_media_identity": {"media_type": "movie"}}
            ]
        },
    )
    assert response.status_code == 400


def test_target_setup_checks_overlap_revision_and_changed_mounts(
    client, targets, tmp_path
):
    movie_id, show_id, movies, _shows = targets
    with pytest.raises(ValueError, match="changed"):
        media_targets.save(movie_id, show_id, 0)
    nested = movies / "shows"
    nested.mkdir()
    nested_id = db.add_custom_path("Nested", str(nested))
    with pytest.raises(ValueError, match="overlapping"):
        media_targets.save(movie_id, nested_id, 1)
    moved = tmp_path / "changed"
    moved.mkdir()
    db.update_custom_path(show_id, path=str(moved))
    with pytest.raises(ValueError, match="mount changed"):
        media_targets.resolve("series")


def test_queue_rechecks_destination_revision_before_writing(client, flat, targets):
    video_library.group_results("filmpalast", flat)
    response = client.post("/api/download", json={"episodes": [flat[0]["url"]]})
    item = db.get_queue_item(response.get_json()["queue_id"])
    media_targets.save(targets[0], targets[1], 1)
    worker._process(item)
    assert db.get_queue_item(item["id"])["status"] == "failed"
    assert not list(targets[3].rglob("*.mkv"))


def test_media_targets_require_admin_and_are_reported_by_capabilities(
    client, api_key, targets
):
    key, _ = api_key("write")
    headers = {"X-API-Key": key}
    assert (
        client.put(
            "/api/media-targets",
            json={"movie": targets[0], "series": targets[1], "revision": 1},
            headers=headers,
        ).status_code
        == 403
    )
    assert (
        client.get("/api/media-targets", headers=headers).get_json()["enabled"] is True
    )
    key, _ = api_key("admin")
    capabilities = client.get(
        "/api/connector/capabilities", headers={"X-API-Key": key}
    ).get_json()
    assert (
        capabilities["video_groups"] == 1
        and capabilities["media_targets"]["series"] == targets[1]
    )


def test_more_than_500_episodes_keep_one_request_and_bounded_work_sections(
    client, flat, targets
):
    rows = [card(f"Long Show S01E{number:03d}") for number in range(1, 502)]
    video_library.group_results("filmpalast", rows)
    response = client.post(
        "/api/download", json={"episodes": [row["url"] for row in rows]}
    )
    assert response.status_code == 200
    item = db.get_queue_item(response.get_json()["queue_id"])
    assert item["total_episodes"] == 501
    assert [
        index for index, _ in worker._queued_entries(json.loads(item["episodes"]), 499)
    ] == [499, 500]


def test_complete_search_follows_real_pagination_and_detects_cycles(monkeypatch):
    first = '<article class="liste"><h2><a href="/stream/silo1" title="SiloS01E01">Silo</a></h2></article><a rel="next" href="/search/title/Silo/2">Next</a>'
    second = '<article class="liste"><h2><a href="/stream/silo2" title="SiloS02E01">Silo</a></h2></article>'
    pages = [first, second]
    monkeypatch.setattr(
        search.GLOBAL_SESSION,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            text=pages.pop(0), raise_for_status=lambda: None
        ),
    )
    result = search.query_filmpalast("Silo", complete=True)
    assert result.complete and len(result) == 2
    monkeypatch.setattr(
        search.GLOBAL_SESSION,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            text=first.replace("/search/title/Silo/2", "/search/title/Silo"),
            raise_for_status=lambda: None,
        ),
    )
    assert not search.query_filmpalast("Silo", complete=True).complete


def test_output_handles_specials_multi_episode_and_portable_names(tmp_path):
    identity = video_library.parse_title("A: Show S00E01-E02")
    assert (
        video_library.output_path(tmp_path, identity)
        == tmp_path / "A- Show/Season 00/A- Show - S00E01-E02.mkv"
    )


def test_structured_parent_ids_keep_same_named_shows_separate():
    first = {
        **card("Episode one", "first"),
        "series_title": "Silo",
        "series_id": "a",
        "season_number": 1,
        "episode_number": 1,
    }
    second = {
        **card("Episode two", "second"),
        "series_title": "Silo",
        "series_id": "b",
        "season_number": 1,
        "episode_number": 1,
    }
    result = video_library.group_results("filmpalast", [first, second])
    assert len(result) == 2 and result[0]["video_id"] != result[1]["video_id"]


def test_sanitized_filename_collision_is_a_conflict(tmp_path):
    first = video_library.parse_title("A: Show S01E01")
    second = video_library.parse_title("A? Show S01E01")
    output = video_library.output_path(tmp_path, first)
    assert output == video_library.output_path(tmp_path, second)
    video_library.reserve_output(output, first)
    with pytest.raises(ValueError, match="different titles"):
        video_library.reserve_output(output, second)


def test_native_duplicate_submission_is_one_active_job_but_can_redownload(
    client, flat, targets
):
    video_library.group_results("filmpalast", flat)
    body = {"episodes": [flat[0]["url"]], "provider": "VOE"}
    first = client.post("/api/download", json=body).get_json()["queue_id"]
    second = client.post("/api/download", json=body).get_json()["queue_id"]
    assert first == second
    db.set_queue_status(first, "completed")
    assert client.post("/api/download", json=body).get_json()["queue_id"] != first


def test_old_queue_conflict_stops_before_any_write(flat, targets):
    video_library.group_results("filmpalast", flat)
    identity = db.add_to_queue(
        "Silo",
        flat[0]["url"],
        [flat[0]["url"]],
        "German Dub",
        "VOE",
        custom_path_id=targets[0],
    )
    worker._process(db.get_queue_item(identity))
    assert db.get_queue_item(identity)["status"] == "failed"
    assert not list(targets[2].rglob("*.mkv")) and not list(targets[3].rglob("*.mkv"))


def test_strict_serial_numbering_never_invents_season_one(targets):
    episode = SimpleNamespace(
        title="A Show", series_episodes=[{"number": 1, "label": "Episode 1"}]
    )
    with pytest.raises(ValueError, match="no season 1 was guessed"):
        api_media._numbered_serials(episode)


def test_native_language_variants_and_existing_foreign_files_are_preserved(
    client, flat, targets
):
    video_library.group_results("filmpalast", flat)
    output = targets[3] / "Silo/Season 01/Silo - S01E01 - german-dub.mkv"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"pre-existing manual file")
    for language in ("German Dub", "English Dub"):
        response = client.post(
            "/api/download",
            json={
                "episodes": [flat[0]["url"]],
                "provider": "VOE",
                "language": language,
            },
        )
        worker._process(db.get_queue_item(response.get_json()["queue_id"]))
    assert output.read_bytes() == b"pre-existing manual file"
    assert output.with_name("Silo - S01E01 - english-dub.mkv").exists()


def test_settings_preview_uses_the_same_typed_roots(client, targets):
    response = client.get(
        "/api/settings/path-preview",
        query_string={"download_path": "a-wrong-legacy-folder"},
    ).get_json()
    assert response["typed"] is True
    assert Path(response["episode"]).is_relative_to(targets[3])
    assert Path(response["movie"]).is_relative_to(targets[2])
    assert "Season 01" in response["episode"] and "S01E01" in response["episode"]


def test_routed_model_path_bypasses_a_broken_legacy_template(monkeypatch, tmp_path):
    from h0melab.models.filmo_to.episode import FilmoEpisode

    monkeypatch.setenv("H0MELAB_NAMING_TEMPLATE", "{invalid}/legacy.mkv")
    episode = FilmoEpisode(
        url="https://filmo.to/movies/test",
        selected_path=str(tmp_path),
        selected_language="German Dub",
    )
    episode._video_identity = video_library.parse_title("SiloS01E01")
    episode._video_root = tmp_path
    episode._connector_variant = "German Dub"
    assert (
        episode._episode_path
        == tmp_path / "Silo/Season 01/Silo - S01E01 - german-dub.mkv"
    )


def test_native_library_keeps_typed_roots_visible_with_legacy_language_separation(
    client, targets, monkeypatch
):
    from h0melab.web import paths

    monkeypatch.setenv("H0MELAB_LANG_SEPARATION", "1")
    folder = targets[3] / "Silo/Season 01"
    folder.mkdir(parents=True)
    (folder / "Silo - S01E01 - german-dub.mkv").write_bytes(b"fixture")
    locations = client.get("/api/library/locations").get_json()["locations"]
    assert any(
        row["custom_path_id"] == targets[1] and row["lang_folder"] is None
        for row in locations
    )
    assert targets[3] in paths.scan_bases()
    assert "Silo" in client.get("/api/downloaded-folders").get_json()["folders"]
