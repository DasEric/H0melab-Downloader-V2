"""Offline regressions for the Gravity Falls report and the connector contract."""

from types import SimpleNamespace

import pytest

from h0melab.catalog.common import CatalogAsset, CatalogItem
from h0melab.web import db, sitesearch
from h0melab.web.search_contract import media_type, relevance, relevant_results
from h0melab.web.views import api_catalog, api_media


def test_checkout_runtime_and_release_versions_agree():
    from pathlib import Path
    from tomllib import loads

    from h0melab.config import VERSION
    from h0melab.version import FALLBACK_VERSION, get_version
    from h0melab.web.version import get_version as web_version

    project = loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert (
        VERSION
        == get_version()
        == web_version()
        == FALLBACK_VERSION
        == project["project"]["version"]
    )


def test_wheel_version_and_missing_package_fallback(monkeypatch, tmp_path):
    from h0melab import version

    monkeypatch.setattr(
        version, "__file__", str(tmp_path / "lib" / "h0melab" / "version.py")
    )
    monkeypatch.setattr(version, "package_version", lambda name: "2.1.0-wheel")
    assert version.get_version() == "2.1.0-wheel"

    def missing(name):
        raise version.PackageNotFoundError(name)

    monkeypatch.setattr(version, "package_version", missing)
    assert version.get_version() == version.FALLBACK_VERSION


@pytest.mark.parametrize(
    "title",
    ["Cat Gravity", "Anti-Gravity Boy", "Gravity Eyes", "True True", "Only For Her"],
)
def test_gravity_falls_rejects_unrelated_titles(title):
    assert relevance({"title": title}, "gravity falls") == 0


def test_gravity_falls_keeps_the_german_title():
    assert relevance({"title": "Willkommen in Gravity Falls"}, "gravity falls") > 0


def test_exact_title_precedes_a_longer_title():
    found = relevant_results(
        [{"title": "Willkommen in Gravity Falls"}, {"title": "Gravity Falls"}],
        "gravity falls",
    )
    assert found[0]["title"] == "Gravity Falls"


def test_aliases_author_year_and_numeric_titles():
    assert relevance(
        {"title": "Anderer Titel", "original_title": "Gravity Falls"}, "gravity falls"
    )
    assert relevance({"title": "Dune", "year": "1984"}, "Dune 2021") == 0
    assert relevance({"title": "Dune", "year": "2021"}, "Dune 2021")
    assert relevance({"title": "1984"}, "1984")
    assert relevance(
        {"title": "Dune", "author": "Frank Herbert", "media_kind": "ebook"}, "Herbert"
    )
    assert relevance({"title": "Dune", "author": "Frank Herbert"}, "Herbert") == 0
    assert relevance({"title": "F1 - Der Film"}, "F1 Movie")


def test_sources_advertise_categories_and_every_browse_adapter(client):
    response = client.get("/api/sources")
    assert response.status_code == 200
    body = response.get_json()
    assert body["schema_version"] == 1
    sources = {item["id"]: item for item in body["sources"]}
    assert sources["mangafire"]["media_types"] == ["manga"]
    assert sources["gutenberg"]["media_types"] == ["ebook"]
    assert sources["librivox"]["media_types"] == ["audiobook"]
    assert sources["playerfm"]["media_types"] == ["podcast"]
    assert "api/filmo-movies" in sources["filmo"]["browse_paths"]
    assert "api/moflix-movies" in sources["moflix"]["browse_paths"]
    assert sources["htv"]["enabled"] is False


def test_series_search_never_calls_mangafire(client, monkeypatch):
    def unexpected(*args):
        pytest.fail("MangaFire was called for a series search")

    monkeypatch.setattr(sitesearch, "search", unexpected)
    assert (
        client.post(
            "/api/search",
            json={
                "keyword": "gravity falls",
                "site": "mangafire",
                "category": "series",
            },
        ).status_code
        == 400
    )


def test_disabled_source_is_not_searched(client, monkeypatch):
    monkeypatch.setenv("H0MELAB_ENABLE_STO", "0")
    assert (
        client.post(
            "/api/search",
            json={"keyword": "gravity falls", "site": "sto", "category": "series"},
        ).status_code
        == 400
    )


def test_search_filters_and_preserves_the_true_category(client, monkeypatch):
    monkeypatch.setattr(
        sitesearch,
        "search",
        lambda *args: [
            {
                "title": "Cat Gravity",
                "url": "https://serienstream.to/serie/cat",
                "poster": "",
                "media_type": "series",
            },
            {
                "title": "Willkommen in Gravity Falls",
                "url": "https://serienstream.to/serie/gravity-falls",
                "poster": "",
                "media_type": "series",
            },
        ],
    )
    body = client.post(
        "/api/search",
        json={"keyword": "gravity falls", "site": "sto", "category": "series"},
    ).get_json()
    assert [item["title"] for item in body["results"]] == [
        "Willkommen in Gravity Falls"
    ]
    assert body["results"][0]["source"] == "sto"
    assert body["results"][0]["relevance"] > 0


def test_missing_title_never_becomes_the_search_query(monkeypatch):
    monkeypatch.setitem(
        sitesearch.SITE_SEARCH, "mangafire", lambda query: [{"url": "/title/unknown"}]
    )
    assert sitesearch.search("mangafire", "gravity falls") == []


def test_mixed_source_keeps_film_and_series_apart(client, monkeypatch):
    monkeypatch.setattr(
        sitesearch,
        "search",
        lambda *args: [
            {
                "title": "Dune",
                "url": "https://example.test/movie/1",
                "poster": "",
                "media_type": "movie",
            },
            {
                "title": "Dune",
                "url": "https://example.test/tv/2",
                "poster": "",
                "media_type": "series",
            },
        ],
    )
    body = client.post(
        "/api/search", json={"keyword": "dune", "site": "moflix", "category": "movie"}
    ).get_json()
    assert len(body["results"]) == 1
    assert body["results"][0]["media_type"] == "movie"


def test_unknown_mixed_type_is_resolved_not_guessed(monkeypatch):
    monkeypatch.setattr(
        api_media,
        "resolve_provider",
        lambda url: SimpleNamespace(
            name="Mixed",
            series_cls=lambda **kwargs: SimpleNamespace(
                seasons=[SimpleNamespace(are_movies=False)]
            ),
        ),
    )
    assert (
        api_media._resolved_media_type("kinox", {"url": "https://example.test/series"})
        == "series"
    )
    assert media_type("mangafire", {}) == "manga"


def test_browse_carries_real_source_and_filters_type(client, monkeypatch):
    monkeypatch.setattr(api_media, "_browse_cache", {})
    view = api_media._make_browse_view(
        "moflix_movies",
        lambda: [
            {
                "title": "Film",
                "url": "https://example.test/film",
                "media_type": "movie",
            },
            {
                "title": "Serie",
                "url": "https://example.test/serie",
                "media_type": "series",
            },
        ],
    )
    with client.application.test_request_context("/api/moflix-movies?category=series"):
        body = view().get_json()
    assert [item["title"] for item in body["results"]] == ["Serie"]
    assert body["results"][0]["source"] == "moflix"


def test_catalog_category_mismatch_is_rejected(client):
    assert (
        client.get(
            "/api/catalog/search?source=gutenberg&q=dune&category=movie"
        ).status_code
        == 400
    )
    assert (
        client.get("/api/catalog/browse?source=gutenberg&category=podcast").status_code
        == 400
    )


def test_selected_asset_downloaded_flag_uses_its_exact_path(
    client, monkeypatch, tmp_path
):
    monkeypatch.setenv("H0MELAB_EBOOK_PATH", str(tmp_path))
    item = CatalogItem(
        "42",
        "gutenberg",
        "ebook",
        "Dune",
        assets=(
            CatalogAsset("epub", "https://example.test/book.epub", "EPUB", "epub"),
            CatalogAsset("pdf", "https://example.test/book.pdf", "PDF", "pdf"),
        ),
    )
    monkeypatch.setattr(
        api_catalog,
        "get_source",
        lambda source: SimpleNamespace(details=lambda id: item),
    )
    from pathlib import Path

    target = Path(api_catalog._target_for(item, item.assets[0]))
    target.parent.mkdir(parents=True)
    target.write_bytes(b"PK-test")
    body = client.get("/api/catalog/details?source=gutenberg&id=42").get_json()
    assert body["assets"][0]["downloaded"] is True
    assert body["assets"][1]["downloaded"] is False


def test_download_retries_use_one_queue_id(client):
    payload = {
        "title": "Example",
        "episodes": ["https://example.test/one"],
        "request_key": "connector-test",
    }
    first = client.post("/api/download", json=payload).get_json()
    second = client.post("/api/download", json=payload).get_json()
    assert first["queue_id"] == second["queue_id"]
    assert len(db.get_queue()) == 1


def test_catalog_download_retries_keep_the_exact_asset(client, monkeypatch):
    item = CatalogItem(
        "42",
        "gutenberg",
        "ebook",
        "Dune",
        assets=(
            CatalogAsset("epub", "https://example.test/dune.epub", "EPUB", "epub"),
        ),
    )
    monkeypatch.setattr(
        api_catalog,
        "get_source",
        lambda source: SimpleNamespace(key="gutenberg", details=lambda id: item),
    )
    payload = {
        "source": "gutenberg",
        "id": "42",
        "asset_id": "epub",
        "request_key": "catalog-retry",
    }
    first = client.post("/api/catalog/download", json=payload).get_json()
    second = client.post("/api/catalog/download", json=payload).get_json()
    assert first["queue_id"] == second["queue_id"]
    assert len(db.get_queue()) == 1
    assert db.get_queue()[0]["source_site"] == "gutenberg"


def test_request_key_serializes_concurrent_submissions():
    from concurrent.futures import ThreadPoolExecutor

    def submit(index):
        return db.add_to_queue(
            "Dune",
            "42",
            ["entry"],
            "en",
            "catalog:gutenberg",
            request_key="concurrent-request",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        queue_ids = list(pool.map(submit, range(4)))
    assert len(set(queue_ids)) == 1
    assert len(db.get_queue()) == 1


@pytest.mark.parametrize(
    "url,category",
    [
        ("/", "series"),
        ("/?category=movie", "movie"),
        ("/books-audio?category=podcast", "podcast"),
    ],
)
def test_pages_have_a_selected_category(client, url, category):
    page = client.get(url).get_data(as_text=True)
    assert 'id="mediaCategory"' in page
    assert f'value="{category}" selected' in page
