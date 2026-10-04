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
    result = media_path(root, LibraryItem("ebook", "Das Buch", "Ada", "2026", extension="epub"))
    assert result == root / "Ada" / "Das Buch (2026)" / "Das Buch (2026).epub"
    assert "Books" not in result.relative_to(root).parts


def test_audiobook_layout_matches_artist_album_track(tmp_path):
    result = media_path(
        tmp_path / "Audiobooks",
        LibraryItem("audiobook", "Werk", "Autor", "", extension="mp3", episode_title="Anfang", track=1),
    )
    assert result.parts[-3:] == ("Autor", "Werk", "01 - Anfang.mp3")


def test_podcast_layout_uses_show_year_and_date(tmp_path):
    result = media_path(
        tmp_path / "Podcasts",
        LibraryItem("podcast", "Sendung", "Herausgeber", extension="mp3", episode_title="Folge", episode_date="2026-10-04"),
    )
    assert result.parts[-3:] == ("Sendung", "2026", "2026-10-04 - Folge.mp3")


def test_names_are_portable():
    assert safe_component('CON', "x") == "_CON"
    assert safe_component('A:B/C*', "x") == "A-B-C"


def test_opf_contains_book_metadata():
    result = opf_bytes(LibraryItem("ebook", "Titel", "Autor", "2026", "de"), identifier="id-1")
    assert b"Titel" in result
    assert b"Autor" in result
    assert b"id-1" in result


def test_descriptions_are_plain_readable_text():
    assert html_to_text("<p>Erste <strong>Zeile</strong></p><p>Zweite</p>") == "Erste Zeile Zweite"
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


def test_catalog_page_and_source_api_are_available(client):
    page = client.get("/books-audio")
    assert page.status_code == 200
    assert b'class="overlay" id="catalogModal"' in page.data
    body = client.get("/api/catalog/sources").get_json()
    assert len(body["sources"]) == 7
    assert set(body["library_paths"]) == {"ebook", "audiobook", "podcast"}


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
    assert client.put("/api/settings", json={"media_library_paths": roots}).status_code == 200
    assert client.get("/api/settings").get_json()["media_library_paths"] == roots


def test_catalog_download_re_resolves_asset_on_server(client, monkeypatch):
    source = get_source("gutenberg")
    item = CatalogItem(
        "42",
        "gutenberg",
        "ebook",
        "The Test Book",
        "Test Author",
        assets=(CatalogAsset("asset-1", "https://gutenberg.org/book.epub", "EPUB", "epub"),),
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
