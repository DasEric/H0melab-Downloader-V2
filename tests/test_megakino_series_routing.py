"""Regression for Tulsa King's real season-title and episode-selector layout."""

from html import escape

import pytest

from h0melab.config import Audio, Subtitles
from h0melab.models.megakino.series import MegaKinoEpisode
from h0melab.providers import resolve_provider
from h0melab.web import db, media_targets, video_library, worker
from h0melab.web.search_contract import media_type

PAGES = [
    (1, "Tulsa King - Staffel 1", "serials/2677-tulsa-king-staffel-1.html"),
    (2, "Tulsa King - Staffel 2", "drama/4675-tulsa-king-staffel-2.html"),
    (3, "Tulsa King - 3 Staffel", "serials/5712-tulsa-king-3-staffel.html"),
    (4, "Tulsa King - 4 Staffel", "serials/6579-tulsa-king-4-staffel.html"),
]
BASE = "https://megakino25.com/"


def markup(title, labels=(), year="2025"):
    html = f'<meta itemprop="name" content="{escape(title, quote=True)}">'
    html += f'<span itemprop="dateCreated"><a>{year}</a></span>'
    if labels:
        html += '<select class="flex-grow-1 se-select">'
        html += "".join(
            f'<option value="ep{n}">{escape(label)}</option>'
            for n, label in enumerate(labels, 1)
        )
        html += "</select>"
        html += "".join(
            f'<select class="mr-select" id="ep{n}">'
            f'<option value="https://voe.sx/e/fixture{n}">VOE</option></select>'
            for n in range(1, len(labels) + 1)
        )
    return html


@pytest.fixture
def source(monkeypatch):
    pages = {
        BASE + slug: markup(title, ["Episode 1", "Episode 2"], str(2021 + season))
        for season, title, slug in PAGES
    }
    pages[BASE + "drama/100-a-film.html"] = markup("A Film", year="2024")
    monkeypatch.setattr(
        MegaKinoEpisode, "_html", property(lambda self: pages[self.url])
    )
    return pages


@pytest.fixture
def destinations(downloads, tmp_path):
    movies = tmp_path / "movies"
    movies.mkdir()
    movie_id = db.add_custom_path("Movies", str(movies), "megakino")
    show_id = db.add_custom_path("Series", str(downloads))
    media_targets.save(movie_id, show_id, 0)
    return movies, downloads


@pytest.mark.parametrize("season,title,slug", PAGES)
def test_tulsa_seasons_and_episodes_use_explicit_page_season(
    client, source, destinations, season, title, slug
):
    url = BASE + slug
    assert media_type("megakino", {"title": title, "url": url}) == "series"
    details = client.get("/api/series", query_string={"url": url})
    assert details.status_code == 200, details.get_json()
    assert details.get_json()["title"] == "Tulsa King"
    assert details.get_json()["release_year"] == ""
    assert details.get_json()["media_type"] == "series"
    response = client.get("/api/seasons", query_string={"url": url})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["seasons"] == [
        {
            "url": url + "#mkep=1",
            "season_number": season,
            "episode_count": 2,
            "are_movies": False,
        }
    ]
    response = client.get("/api/episodes", query_string={"url": url + "#mkep=1"})
    assert response.status_code == 200, response.get_json()
    entries = response.get_json()["episodes"]
    assert [(entry["season_number"], entry["episode_number"]) for entry in entries] == [
        (season, 1),
        (season, 2),
    ]
    for entry in entries:
        identity = video_library.observed(entry["url"])
        assert identity["title"] == "Tulsa King"
        assert (
            identity["year"] == ""
        )  # Season release dates are not series premiere dates.
    selected = MegaKinoEpisode(url=url + "#mkep=2")
    assert (
        "fixture2" in selected.provider_data.get((Audio.GERMAN, Subtitles.NONE))["VOE"]
    )


def test_all_tulsa_seasons_write_one_series_root_not_movie_default(
    client, source, destinations, monkeypatch
):
    movies, shows = destinations

    def download(episode):
        path = episode._episode_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"isolated regression fixture")

    monkeypatch.setattr(MegaKinoEpisode, "download", download)
    for season, _, slug in PAGES:
        url = BASE + slug + "#mkep=2"
        # A direct download must resolve the same identity without a prior details call.
        response = client.post(
            "/api/download",
            json={"episodes": [url], "language": "German Dub", "provider": "VOE"},
        )
        assert response.status_code == 200, response.get_json()
        item = db.get_queue_item(response.get_json()["queue_id"])
        worker._process(item)
        assert db.get_queue_item(item["id"])["status"] == "completed"
        assert (
            shows
            / "Tulsa King"
            / f"Season {season:02d}"
            / f"Tulsa King - S{season:02d}E02 - german-dub.mkv"
        ).is_file()
    assert sorted(path.name for path in shows.iterdir()) == ["Tulsa King"]
    assert not list(movies.rglob("*.mkv"))


def test_movie_in_genre_category_stays_a_movie(client, source, destinations):
    url = BASE + "drama/100-a-film.html"
    assert resolve_provider(url).name == "MegaKino"
    response = client.get("/api/seasons", query_string={"url": url})
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["seasons"][0]["are_movies"] is True
    assert video_library.inspect(url) == {
        "media_type": "movie",
        "title": "A Film",
        "year": "2024",
    }


def test_explicit_episode_numbers_do_not_become_selector_positions(source):
    url = BASE + PAGES[-1][2]
    source[url] = markup("Tulsa King - 4 Staffel", ["Episode 3", "Episode 7"])
    identity = video_library.identity_for_episode(
        MegaKinoEpisode(url=url + "#mkep=2"), url + "#mkep=2"
    )
    assert identity["season"] == 4 and identity["episode"] == 7


@pytest.mark.parametrize(
    "title,labels",
    [
        ("A Show", ["Episode 1"]),
        ("Tulsa King - 4 Staffel", ["Episode"]),
        ("Tulsa King - 4 Staffel", []),
        ("Tulsa King - 4 Staffel", ["S02E01"]),
        ("Tulsa King - 4 Staffel", ["Episode 1", "Episode 1"]),
    ],
)
def test_unverified_or_conflicting_numbering_never_falls_back_to_movies(
    client, source, destinations, title, labels
):
    url = BASE + PAGES[-1][2]
    source[url] = markup(title, labels)
    for endpoint in ("/api/seasons", "/api/episodes"):
        response = client.get(endpoint, query_string={"url": url})
        assert response.status_code == 422, response.get_json()
        assert "error" in response.get_json()
    response = client.post("/api/download", json={"episodes": [url + "#mkep=1"]})
    assert response.status_code != 200
    assert not db.get_queue()


def test_unknown_episode_selector_is_rejected(source):
    url = BASE + PAGES[-1][2] + "#mkep=99"
    with pytest.raises(ValueError, match="episode"):
        video_library.identity_for_episode(MegaKinoEpisode(url=url), url)
