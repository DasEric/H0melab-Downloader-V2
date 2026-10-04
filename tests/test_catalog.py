from types import SimpleNamespace

import pytest

from h0melab.catalog import download as catalog_download
from h0melab.catalog.common import (
    CatalogAsset,
    CatalogError,
    CatalogItem,
    LinkParser,
    html_to_text,
)
from h0melab.catalog.registry import get_source, source_summaries
from h0melab.models.common.library_layout import (
    LibraryItem,
    media_path,
    opf_bytes,
    safe_component,
)
from h0melab.web import db


def test_registry_contains_only_the_requested_sources():
    assert {source["key"] for source in source_summaries()} == {
        "annas_archive",
        "gutenberg",
        "standard_ebooks",
        "audioanarchy",
        "librivox",
        "listennotes",
        "playerfm",
    }


def test_unknown_source_is_rejected():
    with pytest.raises(CatalogError):
        get_source("z-lib")


def test_existing_library_root_is_not_duplicated(tmp_path):
    root = tmp_path / "eBooks"
    result = media_path(
        root, LibraryItem("ebook", "Das Buch", "Ada", "2026", extension="epub")
    )
    assert result == root / "Ada" / "Das Buch (2026)" / "Das Buch (2026).epub"
    assert "Books" not in result.relative_to(root).parts


def test_audiobook_layout_matches_artist_album_track(tmp_path):
    result = media_path(
        tmp_path / "Audiobooks",
        LibraryItem(
            "audiobook",
            "Werk",
            "Autor",
            "",
            extension="mp3",
            episode_title="Anfang",
            track=1,
        ),
    )
    assert result.parts[-3:] == ("Autor", "Werk", "01 - Anfang.mp3")


def test_podcast_layout_uses_show_year_and_date(tmp_path):
    result = media_path(
        tmp_path / "Podcasts",
        LibraryItem(
            "podcast",
            "Sendung",
            "Herausgeber",
            extension="mp3",
            episode_title="Folge",
            episode_date="2026-10-04",
        ),
    )
    assert result.parts[-3:] == ("Sendung", "2026", "2026-10-04 - Folge.mp3")


def test_names_are_portable():
    assert safe_component("CON", "x") == "_CON"
    assert safe_component("A:B/C*", "x") == "A-B-C"


def test_opf_contains_book_metadata():
    result = opf_bytes(
        LibraryItem("ebook", "Titel", "Autor", "2026", "de"), identifier="id-1"
    )
    assert b"Titel" in result
    assert b"Autor" in result
    assert b"id-1" in result


def test_descriptions_are_plain_readable_text():
    assert (
        html_to_text("<p>Erste <strong>Zeile</strong></p><p>Zweite</p>")
        == "Erste Zeile Zweite"
    )
    item = CatalogItem("1", "test", "ebook", "Titel", description="<b>Beschreibung</b>")
    assert item.as_dict()["description"] == "Beschreibung"


def test_html_parser_collects_title_description_and_cover():
    parser = LinkParser("https://example.com/book")
    parser.feed(
        '<title>Fallback</title><meta property="og:title" content="Buchtitel">'
        '<meta name="description" content="Die Beschreibung">'
        '<meta property="og:image" content="/cover.png"><h1>Überschrift</h1>'
    )
    assert parser.title == "Fallback"
    assert parser.metadata["og:title"] == "Buchtitel"
    assert parser.metadata["description"] == "Die Beschreibung"
    assert parser.images[0] == "https://example.com/cover.png"
    assert parser.headings == ["Überschrift"]


def test_html_parser_keeps_links_when_legacy_markup_omits_closing_anchor():
    parser = LinkParser("https://example.com/book/")
    parser.feed('<a href="one.mp3">First<td><a href="two.mp3">Second</a>')
    assert parser.links == [
        ("https://example.com/book/one.mp3", "First"),
        ("https://example.com/book/two.mp3", "Second"),
    ]


def test_catalog_page_and_source_api_are_available(client):
    page = client.get("/books-audio")
    assert page.status_code == 200
    assert b'class="overlay" id="catalogModal"' in page.data
    assert b'id="catalogSourceTrack"' in page.data
    assert b'id="catalogBrowseGrid"' in page.data
    assert b'id="catalogKinds"' not in page.data
    assert b"catalog-search-card" not in page.data
    body = client.get("/api/catalog/sources").get_json()
    assert len(body["sources"]) == 7
    assert set(body["library_paths"]) == {"ebook", "audiobook", "podcast"}


def test_catalog_page_uses_german_interface_setting(client, monkeypatch):
    monkeypatch.setenv("H0MELAB_UI_LANGUAGE", "de")
    page = client.get("/books-audio").get_data(as_text=True)
    assert "eBooks / Hörbücher - H0melab Downloader" in page
    assert "Titel oder Autor suchen …" in page
    assert "Suchen" in page
    assert "Search by title or author" not in page
    assert "Katalog wird geladen …" in page


def test_catalog_page_keeps_english_interface_setting(client, monkeypatch):
    monkeypatch.setenv("H0MELAB_UI_LANGUAGE", "en")
    page = client.get("/books-audio").get_data(as_text=True)
    assert "eBooks / Audio Books - H0melab Downloader" in page
    assert "Search by title or author…" in page
    assert "Titel oder Autor suchen" not in page


def test_catalog_browse_and_random_use_source_catalog(client, monkeypatch):
    from h0melab.web.views import api_catalog

    source = get_source("gutenberg")
    item = CatalogItem("42", "gutenberg", "ebook", "Browse Book", "Author")
    monkeypatch.setattr(source, "browse", lambda page=1: [item])
    browse = client.get("/api/catalog/browse?source=gutenberg").get_json()
    assert browse["items"][0]["title"] == "Browse Book"
    monkeypatch.setattr(api_catalog.random, "randint", lambda start, end: 1)
    monkeypatch.setattr(api_catalog.random, "choice", lambda items: items[0])
    random_item = client.get("/api/catalog/random?source=gutenberg").get_json()
    assert random_item["item"]["id"] == "42"


def test_catalog_random_falls_back_to_first_page(client, monkeypatch):
    from h0melab.web.views import api_catalog

    source = get_source("audioanarchy")
    item = CatalogItem("first", "audioanarchy", "audiobook", "First Book")
    requested_pages = []

    def browse(page=1):
        requested_pages.append(page)
        return [item] if page == 1 else []

    monkeypatch.setattr(source, "browse", browse)
    monkeypatch.setattr(api_catalog.random, "randint", lambda start, end: 3)
    response = client.get("/api/catalog/random?source=audioanarchy")
    assert response.status_code == 200
    assert response.get_json()["item"]["id"] == "first"
    assert requested_pages == [3, 1]


def test_catalog_browse_failure_has_localizable_code(client, monkeypatch):
    source = get_source("gutenberg")
    monkeypatch.setattr(
        source,
        "browse",
        lambda page=1: (_ for _ in ()).throw(CatalogError("upstream failed")),
    )
    response = client.get("/api/catalog/browse?source=gutenberg")
    assert response.status_code == 400
    assert response.get_json()["error_code"] == "catalog_unavailable"


def test_standard_ebooks_assets_use_the_direct_download_marker(monkeypatch):
    source = get_source("standard_ebooks")
    parser = SimpleNamespace(
        links=[
            (
                "https://standardebooks.org/ebooks/author/book/downloads/book.epub",
                "Compatible epub",
            )
        ],
        metadata={"og:title": "Book, by Author - Standard Ebooks"},
        headings=[],
        images=[],
    )
    monkeypatch.setattr(source, "_parse", lambda url: parser)
    item = source.details("https://standardebooks.org/ebooks/author/book")
    assert item.assets[0].url.endswith("book.epub?source=download")


def test_gutenberg_uses_mime_types_for_extensionless_download_urls():
    source = get_source("gutenberg")
    item = source._item(
        {
            "id": 42,
            "title": "Book",
            "formats": {
                "application/epub+zip": "https://www.gutenberg.org/ebooks/42.epub3.images",
                "application/x-mobipocket-ebook": "https://www.gutenberg.org/ebooks/42.kf8.images",
                "application/octet-stream": "https://www.gutenberg.org/cache/epub/42/book.zip",
            },
        }
    )
    assert [(asset.label, asset.extension) for asset in item.assets] == [
        ("EPUB", "epub"),
        ("MOBI", "mobi"),
    ]


def test_audio_anarchy_catalog_uses_album_metadata_and_real_search(monkeypatch):
    from h0melab.catalog import sources

    html = """
      <div id="album">
        <div id="graphic"><img src="letters.gif" alt="Letters Of Insurgents"></div>
        A fictional correspondence about anarchist ideas.
        <div id="subalbum"><a href="letters.html">Download MP3s</a></div>
      </div>
      <div id="album">
        <div id="graphic"><img src="antiwork.jpg" alt="Anti-Work Essays"></div>
        Essays about work and freedom.
        <div id="subalbum"><a href="antiwork.html">Download MP3s</a></div>
      </div>
    """
    monkeypatch.setattr(
        sources,
        "get",
        lambda url, allowed_hosts=(): SimpleNamespace(text=html),
    )
    source = get_source("audioanarchy")
    browse = source.browse()
    assert [item.title for item in browse] == [
        "Letters Of Insurgents",
        "Anti-Work Essays",
    ]
    assert browse[0].cover_url == "https://audioanarchy.org/letters.gif"
    assert browse[0].description == "A fictional correspondence about anarchist ideas."
    assert [item.title for item in source.search("work freedom")] == [
        "Anti-Work Essays"
    ]
    assert source.browse(page=2) == []


def test_listen_notes_details_expose_downloadable_podcast_episodes(monkeypatch):
    from h0melab.catalog import sources

    source = get_source("listennotes")
    monkeypatch.setattr(
        source,
        "_parse",
        lambda url: SimpleNamespace(
            links=[],
            metadata={"og:title": "Show", "og:description": "Description"},
            headings=[],
            images=[],
        ),
    )
    bundle = {
        "title": "Show",
        "author": "Publisher",
        "image": "https://cdn-images.listennotes.com/show.jpg",
        "episodes": [
            {
                "episode_uuid": "episode-1",
                "title": "Episode One",
                "audio_play_url_extension": "https://audio.listennotes.com/e/p/episode-1.mp3",
                "pub_date_ms": 1790913600000,
            }
        ],
    }
    monkeypatch.setattr(
        sources,
        "get",
        lambda url, allowed_hosts=(): SimpleNamespace(
            text=f'<script id="list-bundle" type="application/json">{sources.json.dumps(bundle)}</script>'
        ),
    )
    item = source.details("https://www.listennotes.com/podcasts/show-id/")
    assert item.title == "Show"
    assert item.author == "Publisher"
    assert item.cover_url == "https://cdn-images.listennotes.com/show.jpg"
    assert len(item.assets) == 1
    assert item.assets[0].id == "episode-1"
    assert item.assets[0].title == "Episode One"
    assert item.assets[0].published == "2026-10-02"
    assert item.assets[0].extension == "mp3"


def test_player_fm_json_details_expose_downloadable_episodes(monkeypatch):
    from h0melab.catalog import sources

    payload = {
        "id": 123,
        "title": "Show",
        "author": "Publisher",
        "language": "de",
        "description": "Description",
        "image": {
            "urlBase": "https://cdn.player.fm/images/123/series/cover",
            "suffix": "jpg",
        },
        "episodes": [
            {
                "id": 456,
                "title": "Episode One",
                "url": "https://media.example.com/episode.mp3",
                "publishedAt": 1790913600,
                "mediaType": "audio/mpeg",
                "size": 12345,
            }
        ],
    }
    monkeypatch.setattr(sources, "get_json", lambda url, allowed_hosts=(): payload)
    item = get_source("playerfm").details("https://de.player.fm/series/show-123")
    assert item.title == "Show"
    assert item.author == "Publisher"
    assert item.language == "de"
    assert item.cover_url == "https://cdn.player.fm/images/123/series/cover.jpg"
    assert len(item.assets) == 1
    assert item.assets[0].id == "456"
    assert item.assets[0].title == "Episode One"
    assert item.assets[0].published == "2026-10-02"
    assert item.assets[0].size == 12345


def test_catalog_images_are_proxied_and_verified(client, monkeypatch):
    from h0melab.web.views import api_catalog

    source = get_source("gutenberg")
    item = CatalogItem(
        "42",
        "gutenberg",
        "ebook",
        "Book",
        cover_url="https://gutenberg.org/cover.png",
    )
    monkeypatch.setattr(source, "search", lambda query, page=1: [item])
    body = client.get("/api/catalog/search?source=gutenberg&q=book").get_json()
    cover_url = body["items"][0]["cover_url"]
    assert cover_url.startswith("/api/catalog/cover?token=")
    assert "gutenberg.org" not in cover_url

    image = b"\x89PNG\r\n\x1a\n" + b"x" * 20
    monkeypatch.setattr(api_catalog, "ensure_public_url", lambda url: url)
    monkeypatch.setattr(
        api_catalog,
        "get",
        lambda url, accept="": SimpleNamespace(
            content=image, headers={"content-type": "image/png"}
        ),
    )
    response = client.get(cover_url)
    assert response.status_code == 200
    assert response.content_type == "image/png"
    assert response.data == image
    assert client.get("/api/catalog/cover?token=broken").status_code == 400


def test_media_library_paths_can_be_saved(client, tmp_path):
    roots = {
        "ebook": str(tmp_path / "my-books"),
        "audiobook": str(tmp_path / "my-audio"),
        "podcast": str(tmp_path / "my-podcasts"),
    }
    assert (
        client.put("/api/settings", json={"media_library_paths": roots}).status_code
        == 200
    )
    assert client.get("/api/settings").get_json()["media_library_paths"] == roots


def test_catalog_download_re_resolves_asset_on_server(client, monkeypatch):
    source = get_source("gutenberg")
    item = CatalogItem(
        "42",
        "gutenberg",
        "ebook",
        "The Test Book",
        "Test Author",
        assets=(
            CatalogAsset("asset-1", "https://gutenberg.org/book.epub", "EPUB", "epub"),
        ),
    )
    monkeypatch.setattr(source, "details", lambda item_id: item)
    response = client.post(
        "/api/catalog/download",
        json={"source": "gutenberg", "id": "42", "asset_id": "asset-1"},
    )
    assert response.status_code == 200
    queued = db.get_queue_item(response.get_json()["queue_id"])
    assert queued["source"] == "catalog"
    assert queued["media_kind"] == "ebook"
    assert queued["source_site"] == "gutenberg"


def test_selected_format_has_its_own_target_path(client, monkeypatch, tmp_path):
    monkeypatch.setenv("H0MELAB_EBOOK_PATH", str(tmp_path / "Books"))
    source = get_source("gutenberg")
    item = CatalogItem(
        "42",
        "gutenberg",
        "ebook",
        "Book",
        "Author",
        assets=(
            CatalogAsset("pdf", "https://gutenberg.org/book.pdf", "PDF", "pdf"),
            CatalogAsset("epub", "https://gutenberg.org/book.epub", "EPUB", "epub"),
        ),
    )
    monkeypatch.setattr(source, "details", lambda item_id: item)
    details = client.get("/api/catalog/details?source=gutenberg&id=42").get_json()
    assert details["assets"][0]["target_path"].endswith("Book.pdf")
    assert details["assets"][1]["target_path"].endswith("Book.epub")
    queued = client.post(
        "/api/catalog/download",
        json={"source": "gutenberg", "id": "42", "asset_id": "epub"},
    ).get_json()
    assert queued["target_path"].endswith("Book.epub")


def test_download_writes_media_description_and_real_cover_format(monkeypatch, tmp_path):
    root = tmp_path / "Books"
    monkeypatch.setenv("H0MELAB_EBOOK_PATH", str(root))

    def fake_stream(queue_id, url, target, **kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        if kwargs.get("expected") == "image":
            target.write_bytes(b"\x89PNG\r\n\x1a\n" + b"cover")
        else:
            target.write_bytes(b"PK\x03\x04" + b"ebook")
        return url

    monkeypatch.setattr(catalog_download, "_stream", fake_stream)
    entry = {
        "item": {
            "id": "book-1",
            "media_kind": "ebook",
            "title": "Titel",
            "author": "Autor",
            "year": "2026",
            "language": "de",
            "description": "Lesbare Beschreibung",
            "cover_url": "https://example.com/cover.png",
        },
        "asset": {
            "url": "https://example.com/book.epub",
            "extension": "epub",
        },
    }
    installed = catalog_download.download_entry(123, entry)
    media = root / "Autor" / "Titel (2026)" / "Titel (2026).epub"
    assert installed == [str(media)]
    assert media.read_bytes().startswith(b"PK\x03\x04")
    assert (media.parent / "cover.png").read_bytes().startswith(b"\x89PNG")
    metadata = (media.parent / "metadata.opf").read_text(encoding="utf-8")
    assert "Lesbare Beschreibung" in metadata
    assert "book-1" in metadata


def test_client_cannot_supply_a_download_url(client, monkeypatch):
    source = get_source("gutenberg")
    item = CatalogItem("42", "gutenberg", "ebook", "Book", assets=())
    monkeypatch.setattr(source, "details", lambda item_id: item)
    response = client.post(
        "/api/catalog/download",
        json={
            "source": "gutenberg",
            "id": "42",
            "asset_id": "https://example.test/attacker-selected.epub",
        },
    )
    assert response.status_code == 400
