"""Retired sources cannot be re-enabled by stale configuration or direct URLs."""

import pytest

from h0melab.providers import resolve_provider
from h0melab.web import media, settings_store, sitesearch
from h0melab.web.search_contract import source_capabilities


@pytest.mark.parametrize("site", ["burningseries", "kinox", "cineby"])
def test_retired_provider_is_not_advertised_or_enabled(site, monkeypatch, client):
    monkeypatch.setenv(f"H0MELAB_ENABLE_{site.upper()}", "1")
    assert not settings_store.site_enabled(site)
    assert site not in media.SITE_KEYS
    assert site not in sitesearch.SITE_SEARCH
    assert site not in sitesearch.SERIES_SITES
    assert site not in sitesearch.MOVIE_SITES
    assert f"enable_{site}" not in settings_store.read_settings()
    assert site not in {row["id"] for row in source_capabilities()["sources"]}
    assert f'data-site="{site}"' not in client.get("/").get_data(as_text=True)
    assert f'data-setting="enable_{site}"' not in client.get("/settings").get_data(
        as_text=True
    )
    assert site.upper() not in settings_store.export_env()


@pytest.mark.parametrize(
    "url",
    [
        "https://bs.to/serie/Example",
        "https://burning-series.io/serie/Example/1/episode/de",
        "https://kinox.to/Stream/Example.html",
        "https://www.cineby.at/movie/157336",
        "https://www.cineby.app/tv/1405/1/1",
    ],
)
def test_retired_provider_direct_urls_are_unsupported(url):
    with pytest.raises(ValueError, match="Unsupported URL"):
        resolve_provider(url)


@pytest.mark.parametrize(
    "path", ["/api/kinox-movies", "/api/burningseries-series", "/api/cineby-movies"]
)
def test_retired_browse_routes_are_removed(client, path):
    assert client.get(path).status_code == 404
