import pytest
from types import SimpleNamespace
from h0melab.models.broadcasters import browse
from h0melab.web.views import api_media


def test_zdf_cards_filter_and_deduplicate():
    parser = browse.ZDFCards()
    card = '<a href="/serien/example-100"><img src="https://www.zdf.de/assets/a.jpg"><h3>Example &amp; More</h3></a>'
    parser.feed(
        card
        + card
        + '<a href="https://evil.test/example-100"><img src="https://evil.test/a.jpg"><h3>Wrong</h3></a>'
    )
    assert len(parser.rows) == 1
    assert parser.rows[0]["title"] == "Example & More"
    assert parser.rows[0]["poster_url"].startswith("https://www.zdf.de/")


def test_zdf_fetch_checks_status_timeout(monkeypatch):
    def get(url, **kw):
        assert url == "https://www.zdf.de/" and kw["timeout"] == 15
        return SimpleNamespace(
            raise_for_status=lambda: None,
            text='<a href="/serien/example-100"><img src="https://www.zdf.de/a.jpg"><h3>Example</h3></a>',
        )

    monkeypatch.setattr(browse.niquests, "get", get)
    assert len(browse.fetch_zdf_preview()) == 1


def test_dmax_bounded_catalogue(monkeypatch):
    def site(self, path, **kw):
        assert path == "search/page/" and kw["page[size]"] == 24
        return {
            "data": [
                {
                    "title": "Show",
                    "slug": "show",
                    "parentSlug": "sendungen",
                    "image": {"url": "https://cdn.example/a.jpg"},
                }
            ]
            * 30
        }

    monkeypatch.setattr(browse.DMAXClient, "site", site)
    rows = browse.fetch_dmax_preview()
    assert len(rows) == 1 and rows[0]["poster_url"]


@pytest.mark.parametrize("site", ["zdf", "dmax"])
def test_preview_http_and_template(client, monkeypatch, site):
    api_media._browse_cache.clear()
    monkeypatch.setattr(
        api_media,
        "_cached",
        lambda key, fetch, **kw: [
            {
                "title": "Show",
                "url": f"https://{site}.de/serien/show-100",
                "poster_url": f"https://{site}.de/a.jpg",
            }
        ],
    )
    response = client.get(f"/api/{site}-preview")
    assert response.status_code == 200
    row = response.get_json()["results"][0]
    assert row["source"] == site and row["poster_url"]
    monkeypatch.setenv("H0MELAB_ENABLE_ANIWORLD", "0")
    monkeypatch.setenv("H0MELAB_ENABLE_" + site.upper(), "1")
    assert f'data-row="{site}_preview"' in client.get("/").get_data(as_text=True)
