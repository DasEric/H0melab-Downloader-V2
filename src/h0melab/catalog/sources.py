"""Concrete catalog sources.

Public APIs/feeds are preferred.  HTML sources use conservative link parsing so
markup changes produce an empty result instead of an unsafe guessed download.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from .common import (
    CatalogAsset,
    CatalogError,
    CatalogItem,
    CatalogSource,
    LinkParser,
    extension_from_url,
    get,
    get_json,
    html_to_text,
)

_DOWNLOAD_EXTENSIONS = {"epub", "pdf", "mobi", "azw3", "mp3", "m4a", "m4b", "flac", "ogg", "opus", "zip"}


class GutenbergSource(CatalogSource):
    key, label, media_kind = "gutenberg", "Project Gutenberg", "ebook"
    hosts = ("gutendex.com", "gutenberg.org")

    def _item(self, book):
        author = next((a.get("name", "") for a in book.get("authors", []) if a.get("name")), "")
        assets = []
        cover = ""
        for content_type, url in book.get("formats", {}).items():
            if not url:
                continue
            if content_type.startswith("image/") and not cover:
                cover = url
            extension = extension_from_url(url)
            if extension in _DOWNLOAD_EXTENSIONS and "text/html" not in content_type:
                assets.append(CatalogAsset(url, url, extension.upper(), extension, content_type))
        return CatalogItem(
            id=str(book.get("id", "")), source=self.key, media_kind=self.media_kind,
            title=book.get("title") or "Unknown title", author=author,
            language=(book.get("languages") or [""])[0], cover_url=cover,
            assets=tuple(assets),
        )

    def validate_id(self, item_id):
        if not str(item_id).isdigit():
            raise CatalogError("Invalid Gutenberg identifier")
        return str(item_id)

    def search(self, query, *, page=1):
        data = get_json(f"https://gutendex.com/books?{urlencode({'search': query, 'page': page})}", allowed_hosts=("gutendex.com",))
        return [self._item(book) for book in data.get("results", [])]

    def details(self, item_id):
        item_id = self.validate_id(item_id)
        data = get_json(f"https://gutendex.com/books/{item_id}", allowed_hosts=("gutendex.com",))
        return self._item(data)


class LibriVoxSource(CatalogSource):
    key, label, media_kind = "librivox", "LibriVox", "audiobook"
    hosts = ("librivox.org", "archive.org")

    def _item(self, book):
        authors = book.get("authors") or []
        author = " ".join(filter(None, ((authors[0].get("first_name", "") if authors else ""), (authors[0].get("last_name", "") if authors else ""))))
        assets = []
        zip_url = book.get("url_zip_file") or ""
        if zip_url:
            assets.append(CatalogAsset("zip", zip_url, "MP3 ZIP", "zip", "application/zip"))
        return CatalogItem(
            id=str(book.get("id", "")), source=self.key, media_kind=self.media_kind,
            title=book.get("title") or "Unknown title", author=author,
            language=book.get("language") or "", description=html_to_text(book.get("description")),
            cover_url=book.get("url_cover") or book.get("coverart_jpg") or "", assets=tuple(assets),
        )

    def validate_id(self, item_id):
        if not str(item_id).isdigit():
            raise CatalogError("Invalid LibriVox identifier")
        return str(item_id)

    def _api(self, **params):
        params.update({"format": "json", "extended": "1"})
        return get_json(f"https://librivox.org/api/feed/audiobooks/?{urlencode(params)}", allowed_hosts=("librivox.org",))

    def search(self, query, *, page=1):
        data = self._api(title=query, limit=20, offset=max(0, page - 1) * 20)
        return [self._item(book) for book in data.get("books", [])]

    def details(self, item_id):
        item_id = self.validate_id(item_id)
        books = self._api(id=item_id).get("books", [])
        if not books:
            raise CatalogError("LibriVox item was not found")
        return self._item(books[0])


class StandardEbooksSource(CatalogSource):
    key, label, media_kind = "standard_ebooks", "Standard Ebooks", "ebook"
    hosts = ("standardebooks.org",)
    search_url = "https://standardebooks.org/ebooks?query={query}&page={page}"

    def _parse(self, url):
        response = get(url, allowed_hosts=self.hosts)
        parser = LinkParser(url)
        parser.feed(response.text)
        return parser

    def search(self, query, *, page=1):
        parser = self._parse(
            self.search_url.format(query=quote(query), page=max(1, int(page)))
        )
        seen, output = set(), []
        for url, text in parser.links:
            path = url.split("standardebooks.org", 1)[-1]
            if (
                not text
                or not path.startswith("/ebooks/")
                or path.count("/") < 3
                or url in seen
            ):
                continue
            if any(segment in path for segment in ("/downloads/", ".epub", ".azw3")):
                continue
            seen.add(url)
            author_slug = path.strip("/").split("/")[1]
            author = author_slug.replace("-", " ").title()
            output.append(
                CatalogItem(url, self.key, self.media_kind, text, author=author)
            )
            if len(output) >= 30:
                break
        return output

    def details(self, item_id):
        item_id = self.validate_id(item_id)
        parser = self._parse(item_id)
        assets = []
        for url, label in parser.links:
            extension = extension_from_url(url)
            if extension in {"epub", "azw3", "kepub"}:
                # Standard Ebooks deliberately serves an intermediate HTML page
                # unless its documented download marker is present.
                download_url = f"{url}{'&' if '?' in url else '?'}source=download"
                assets.append(
                    CatalogAsset(
                        download_url,
                        download_url,
                        label or extension.upper(),
                        extension,
                    )
                )
        slug = item_id.rstrip("/").rsplit("/", 1)[-1]
        title = parser.metadata.get("og:title") or (parser.headings[0] if parser.headings else "") or slug.replace("-", " ")
        description = parser.metadata.get("og:description") or parser.metadata.get("description", "")
        author = parser.metadata.get("author", "")
        if ", by " in title:
            title, credited = title.split(", by ", 1)
            author = credited.split(" - ", 1)[0].strip()
        language = parser.metadata.get("schema:inlanguage", "")
        year = parser.metadata.get("schema:datepublished", "")[:4]
        return CatalogItem(item_id, self.key, self.media_kind, title, author=author, year=year, language=language, description=description, cover_url=(parser.images[0] if parser.images else ""), assets=tuple(dict.fromkeys(assets)))


class HtmlCatalogSource(CatalogSource):
    search_url = ""
    item_path_markers: tuple[str, ...] = ()

    def _parse(self, url):
        response = get(url, allowed_hosts=self.hosts)
        parser = LinkParser(url)
        parser.feed(response.text)
        return parser

    def _is_item(self, url):
        return any(marker in url for marker in self.item_path_markers)

    def search(self, query, *, page=1):
        parser = self._parse(self.search_url.format(query=quote(query), page=max(1, int(page))))
        seen, items = set(), []
        for url, text in parser.links:
            if url in seen or not text or not self._is_item(url):
                continue
            try:
                self.validate_id(url)
            except CatalogError:
                continue
            seen.add(url)
            items.append(CatalogItem(url, self.key, self.media_kind, text[:240]))
            if len(items) >= 30:
                break
        return items

    def details(self, item_id):
        url = self.validate_id(item_id)
        parser = self._parse(url)
        assets = []
        for index, (link, label) in enumerate(parser.links, 1):
            extension = extension_from_url(link)
            if extension in _DOWNLOAD_EXTENSIONS:
                assets.append(
                    CatalogAsset(
                        link,
                        link,
                        label or extension.upper(),
                        extension,
                        title=label,
                        track=index,
                    )
                )
        fallback = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
        title = parser.metadata.get("og:title") or (parser.headings[0] if parser.headings else "") or fallback
        description = parser.metadata.get("og:description") or parser.metadata.get("description", "")
        author = parser.metadata.get("author", "")
        return CatalogItem(url, self.key, self.media_kind, title, author=author, description=description, cover_url=(parser.images[0] if parser.images else ""), assets=tuple(dict.fromkeys(assets)))


class AnnasArchiveSource(HtmlCatalogSource):
    key, label, media_kind = "annas_archive", "Anna’s Archive", "ebook"
    hosts = ("annas-archive.gl", "annas-archive.pk", "annas-archive.gd")
    search_url = "https://annas-archive.gl/search?q={query}&page={page}"
    item_path_markers = ("/md5/",)


class AudioAnarchySource(HtmlCatalogSource):
    key, label, media_kind = "audioanarchy", "Audio Anarchy", "audiobook"
    hosts = ("audioanarchy.org",)
    search_url = "https://audioanarchy.org/?s={query}&paged={page}"
    item_path_markers = ("audioanarchy.org/",)

    def _is_item(self, url):
        return super()._is_item(url) and "?s=" not in url and not url.rstrip("/").endswith("audioanarchy.org")


class ListenNotesSource(HtmlCatalogSource):
    key, label, media_kind = "listennotes", "Listen Notes", "podcast"
    hosts = ("listennotes.com",)
    search_url = "https://www.listennotes.com/search/?q={query}&scope=podcast&page={page}"
    item_path_markers = ("/podcasts/", "/e/")


class PlayerFMSource(HtmlCatalogSource):
    key, label, media_kind = "playerfm", "Player FM", "podcast"
    hosts = ("player.fm", "de.player.fm")
    search_url = "https://de.player.fm/search/{query}?page={page}"
    item_path_markers = ("/series/",)
