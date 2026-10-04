"""Concrete catalog sources.

Public APIs/feeds are preferred.  HTML sources use conservative link parsing so
markup changes produce an empty result instead of an unsafe guessed download.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urljoin, urlparse

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

_DOWNLOAD_EXTENSIONS = {
    "epub",
    "pdf",
    "mobi",
    "azw3",
    "mp3",
    "m4a",
    "m4b",
    "flac",
    "ogg",
    "opus",
    "zip",
}

_GUTENBERG_CONTENT_EXTENSIONS = {
    "application/epub+zip": "epub",
    "application/pdf": "pdf",
    "application/x-mobipocket-ebook": "mobi",
}


class _AudioAnarchyIndexParser(HTMLParser):
    """Extract the old-style album blocks without relying on repeated link text."""

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.entries = []
        self._depth = 0
        self._entry = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "div" and values.get("id") == "album" and not self._depth:
            self._depth = 1
            self._entry = {"id": "", "title": "", "cover_url": "", "text": []}
            return
        if not self._entry:
            return
        if tag == "div":
            self._depth += 1
        elif tag == "img" and values.get("alt"):
            self._entry["title"] = values["alt"].strip()
            self._entry["cover_url"] = urljoin(self.base_url, values.get("src", ""))
        elif tag == "a" and values.get("href"):
            target = urljoin(self.base_url, values["href"])
            if urlparse(target).path.lower().endswith(".html"):
                self._entry["id"] = target
        elif tag in {"br", "p"}:
            self._entry["text"].append(" ")

    def handle_data(self, data):
        if self._entry:
            self._entry["text"].append(data)

    def handle_endtag(self, tag):
        if tag != "div" or not self._entry:
            return
        self._depth -= 1
        if self._depth:
            return
        description = html_to_text(" ".join(self._entry.pop("text")))
        description = re.sub(r"\s*Download MP3s\s*$", "", description).strip()
        self._entry["description"] = description
        if self._entry["id"] and self._entry["title"]:
            self.entries.append(self._entry)
        self._entry = None


class GutenbergSource(CatalogSource):
    key, label, media_kind = "gutenberg", "Project Gutenberg", "ebook"
    hosts = ("gutendex.com", "gutenberg.org")

    def _item(self, book):
        author = next(
            (a.get("name", "") for a in book.get("authors", []) if a.get("name")), ""
        )
        assets = []
        cover = ""
        for content_type, url in book.get("formats", {}).items():
            if not url:
                continue
            if content_type.startswith("image/") and not cover:
                cover = url
            mime_type = content_type.split(";", 1)[0].lower()
            extension = _GUTENBERG_CONTENT_EXTENSIONS.get(
                mime_type, extension_from_url(url)
            )
            if extension in {"epub", "pdf", "mobi", "azw3"}:
                assets.append(
                    CatalogAsset(url, url, extension.upper(), extension, content_type)
                )
        return CatalogItem(
            id=str(book.get("id", "")),
            source=self.key,
            media_kind=self.media_kind,
            title=book.get("title") or "Unknown title",
            author=author,
            language=(book.get("languages") or [""])[0],
            cover_url=cover,
            assets=tuple(assets),
        )

    def validate_id(self, item_id):
        if not str(item_id).isdigit():
            raise CatalogError("Invalid Gutenberg identifier")
        return str(item_id)

    def search(self, query, *, page=1):
        data = get_json(
            f"https://gutendex.com/books?{urlencode({'search': query, 'page': page})}",
            allowed_hosts=("gutendex.com",),
        )
        return [self._item(book) for book in data.get("results", [])]

    def browse(self, *, page=1):
        data = get_json(
            f"https://gutendex.com/books/?{urlencode({'page': page})}",
            allowed_hosts=("gutendex.com",),
        )
        return [self._item(book) for book in data.get("results", [])]

    def details(self, item_id):
        item_id = self.validate_id(item_id)
        data = get_json(
            f"https://gutendex.com/books/{item_id}", allowed_hosts=("gutendex.com",)
        )
        return self._item(data)


class LibriVoxSource(CatalogSource):
    key, label, media_kind = "librivox", "LibriVox", "audiobook"
    hosts = ("librivox.org", "archive.org")

    def _item(self, book):
        authors = book.get("authors") or []
        author = " ".join(
            filter(
                None,
                (
                    (authors[0].get("first_name", "") if authors else ""),
                    (authors[0].get("last_name", "") if authors else ""),
                ),
            )
        )
        assets = []
        zip_url = book.get("url_zip_file") or ""
        if zip_url:
            assets.append(
                CatalogAsset("zip", zip_url, "MP3 ZIP", "zip", "application/zip")
            )
        return CatalogItem(
            id=str(book.get("id", "")),
            source=self.key,
            media_kind=self.media_kind,
            title=book.get("title") or "Unknown title",
            author=author,
            language=book.get("language") or "",
            description=html_to_text(book.get("description")),
            cover_url=book.get("url_cover") or book.get("coverart_jpg") or "",
            assets=tuple(assets),
        )

    def validate_id(self, item_id):
        if not str(item_id).isdigit():
            raise CatalogError("Invalid LibriVox identifier")
        return str(item_id)

    def _api(self, **params):
        params.update({"format": "json", "extended": "1"})
        return get_json(
            f"https://librivox.org/api/feed/audiobooks/?{urlencode(params)}",
            allowed_hosts=("librivox.org",),
        )

    def search(self, query, *, page=1):
        data = self._api(title=query, limit=20, offset=max(0, page - 1) * 20)
        return [self._item(book) for book in data.get("books", [])]

    def browse(self, *, page=1):
        data = self._api(limit=20, offset=max(0, page - 1) * 20)
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

    def browse(self, *, page=1):
        return self.search("", page=page)

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
        title = (
            parser.metadata.get("og:title")
            or (parser.headings[0] if parser.headings else "")
            or slug.replace("-", " ")
        )
        description = parser.metadata.get("og:description") or parser.metadata.get(
            "description", ""
        )
        author = parser.metadata.get("author", "")
        if ", by " in title:
            title, credited = title.split(", by ", 1)
            author = credited.split(" - ", 1)[0].strip()
        language = parser.metadata.get("schema:inlanguage", "")
        year = parser.metadata.get("schema:datepublished", "")[:4]
        return CatalogItem(
            item_id,
            self.key,
            self.media_kind,
            title,
            author=author,
            year=year,
            language=language,
            description=description,
            cover_url=(parser.images[0] if parser.images else ""),
            assets=tuple(dict.fromkeys(assets)),
        )


class HtmlCatalogSource(CatalogSource):
    search_url = ""
    browse_url = ""
    item_path_markers: tuple[str, ...] = ()

    def _parse(self, url):
        response = get(url, allowed_hosts=self.hosts)
        parser = LinkParser(url)
        parser.feed(response.text)
        return parser

    def _is_item(self, url):
        return any(marker in url for marker in self.item_path_markers)

    def search(self, query, *, page=1):
        parser = self._parse(
            self.search_url.format(query=quote(query), page=max(1, int(page)))
        )
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

    def browse(self, *, page=1):
        url = (self.browse_url or self.search_url).format(
            query="", page=max(1, int(page))
        )
        parser = self._parse(url)
        seen, items = set(), []
        for item_url, text in parser.links:
            if item_url in seen or not text or not self._is_item(item_url):
                continue
            try:
                self.validate_id(item_url)
            except CatalogError:
                continue
            seen.add(item_url)
            items.append(CatalogItem(item_url, self.key, self.media_kind, text[:240]))
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
        title = (
            parser.metadata.get("og:title")
            or (parser.headings[0] if parser.headings else "")
            or fallback
        )
        description = parser.metadata.get("og:description") or parser.metadata.get(
            "description", ""
        )
        author = parser.metadata.get("author", "")
        return CatalogItem(
            url,
            self.key,
            self.media_kind,
            title,
            author=author,
            description=description,
            cover_url=(parser.images[0] if parser.images else ""),
            assets=tuple(dict.fromkeys(assets)),
        )


class AnnasArchiveSource(HtmlCatalogSource):
    key, label, media_kind = "annas_archive", "Anna’s Archive", "ebook"
    hosts = ("annas-archive.gl", "annas-archive.pk", "annas-archive.gd")
    search_url = "https://annas-archive.gl/search?q={query}&page={page}"
    browse_url = "https://annas-archive.gl/search?page={page}"
    item_path_markers = ("/md5/",)


class AudioAnarchySource(HtmlCatalogSource):
    key, label, media_kind = "audioanarchy", "Audio Anarchy", "audiobook"
    hosts = ("audioanarchy.org",)
    search_url = "https://audioanarchy.org/"
    browse_url = "https://audioanarchy.org/"
    item_path_markers = (".html",)

    def _is_item(self, url):
        return super()._is_item(url) and urlparse(url).path.lower().endswith(".html")

    def _index_items(self):
        response = get(self.browse_url, allowed_hosts=self.hosts)
        parser = _AudioAnarchyIndexParser(self.browse_url)
        parser.feed(response.text)
        return [
            CatalogItem(
                entry["id"],
                self.key,
                self.media_kind,
                entry["title"],
                description=entry["description"],
                cover_url=entry["cover_url"],
            )
            for entry in parser.entries
        ]

    def browse(self, *, page=1):
        return self._index_items() if int(page) == 1 else []

    def search(self, query, *, page=1):
        if int(page) != 1:
            return []
        terms = [term for term in str(query).casefold().split() if term]
        return [
            item
            for item in self._index_items()
            if all(
                term in f"{item.title} {item.description}".casefold() for term in terms
            )
        ]

    def details(self, item_id):
        item_id = self.validate_id(item_id)
        if not self._is_item(item_id):
            raise CatalogError("Invalid Audio Anarchy identifier")
        details = super().details(item_id)
        summary = next(
            (item for item in self._index_items() if item.id == item_id), None
        )
        if summary is None:
            return details
        return CatalogItem(
            item_id,
            self.key,
            self.media_kind,
            summary.title,
            author=details.author,
            year=details.year,
            language=details.language,
            description=summary.description or details.description,
            cover_url=summary.cover_url or details.cover_url,
            assets=details.assets,
        )


class ListenNotesSource(HtmlCatalogSource):
    key, label, media_kind = "listennotes", "Listen Notes", "podcast"
    hosts = ("listennotes.com",)
    search_url = (
        "https://www.listennotes.com/search/?q={query}&scope=podcast&page={page}"
    )
    browse_url = "https://www.listennotes.com/best-podcasts/"
    item_path_markers = ("/podcasts/", "/e/")

    def details(self, item_id):
        item_id = self.validate_id(item_id)
        details = super().details(item_id)
        embed_url = f"{item_id.rstrip('/')}/embed/"
        response = get(embed_url, allowed_hosts=self.hosts)
        bundle = re.search(
            r'<script[^>]+id=["\']list-bundle["\'][^>]*>(.*?)</script>',
            response.text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if bundle is None:
            return details
        try:
            payload = json.loads(bundle.group(1).strip())
        except (TypeError, ValueError) as exc:
            raise CatalogError("Listen Notes returned invalid episode data") from exc
        assets = []
        for index, episode in enumerate(payload.get("episodes") or [], start=1):
            url = episode.get("audio_play_url_extension") or episode.get("audio") or ""
            title = str(episode.get("title") or "").strip()
            if not url or not title:
                continue
            published = ""
            try:
                published = (
                    datetime.fromtimestamp(
                        int(episode.get("pub_date_ms")) / 1000, tz=UTC
                    )
                    .date()
                    .isoformat()
                )
            except (TypeError, ValueError, OSError, OverflowError):
                pass
            asset_id = str(episode.get("episode_uuid") or url)
            assets.append(
                CatalogAsset(
                    asset_id,
                    url,
                    title,
                    "mp3",
                    "audio/mpeg",
                    title=title,
                    published=published,
                    track=index,
                )
            )
        return CatalogItem(
            item_id,
            self.key,
            self.media_kind,
            payload.get("title") or details.title,
            author=payload.get("author") or details.author,
            year=details.year,
            language=details.language,
            description=details.description,
            cover_url=payload.get("image") or details.cover_url,
            assets=tuple(assets),
        )


class PlayerFMSource(HtmlCatalogSource):
    key, label, media_kind = "playerfm", "Player FM", "podcast"
    hosts = ("player.fm", "de.player.fm")
    search_url = (
        "https://de.player.fm/search/{query}.json?episode_detail=full&page={page}"
    )
    browse_url = "https://de.player.fm/featured/education.json?episode_detail=full"
    item_path_markers = ("/series/",)

    @staticmethod
    def _cover(image):
        image = image or {}
        url = str(image.get("url") or "")
        if url.startswith("https://"):
            return url
        base = str(image.get("urlBase") or "")
        suffix = str(image.get("suffix") or "jpg").lstrip(".")
        return f"{base}.{suffix}" if base.startswith("https://") else ""

    def _series_item(self, series):
        identifier = str(series.get("id") or "")
        if not identifier:
            raise CatalogError("Player FM series has no identifier")
        network = series.get("network") or {}
        return CatalogItem(
            f"https://de.player.fm/series/{identifier}",
            self.key,
            self.media_kind,
            series.get("title") or "Unknown title",
            author=series.get("author") or network.get("name") or "",
            language=series.get("language") or "",
            description=series.get("description") or "",
            cover_url=self._cover(series.get("image")),
        )

    def browse(self, *, page=1):
        categories = ("education", "news", "science")
        page = max(1, int(page))
        if page > len(categories):
            return []
        data = get_json(
            f"https://de.player.fm/featured/{categories[page - 1]}.json?episode_detail=full",
            allowed_hosts=self.hosts,
        )
        seen, items = set(), []
        for episode in data.get("episodes") or []:
            series = episode.get("series") or {}
            identifier = str(series.get("id") or "")
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            items.append(self._series_item(series))
            if len(items) >= 30:
                break
        return items

    def search(self, query, *, page=1):
        parser = self._parse(
            self.search_url.format(query=quote(query), page=max(1, int(page)))
        )
        seen, items = set(), []
        for url, text in parser.links:
            path = urlparse(url).path.strip("/").split("/")
            if len(path) != 2 or path[0] != "series" or not text:
                continue
            identifier = path[1]
            if identifier.isdigit() or identifier in seen:
                continue
            seen.add(identifier)
            items.append(CatalogItem(url, self.key, self.media_kind, text))
            if len(items) >= 30:
                break
        return items

    def details(self, item_id):
        item_id = self.validate_id(item_id)
        path = urlparse(item_id).path.rstrip("/")
        if not path.startswith("/series/") or path.count("/") != 2:
            raise CatalogError("Invalid Player FM series identifier")
        data = get_json(
            f"https://de.player.fm{path}.json?episode_detail=full",
            allowed_hosts=self.hosts,
        )
        item = self._series_item(data)
        assets = []
        for index, episode in enumerate(data.get("episodes") or [], start=1):
            url = str(episode.get("url") or "")
            title = str(episode.get("title") or "").strip()
            if not url or not title:
                continue
            published = ""
            try:
                published = (
                    datetime.fromtimestamp(int(episode.get("publishedAt")), tz=UTC)
                    .date()
                    .isoformat()
                )
            except (TypeError, ValueError, OSError, OverflowError):
                pass
            assets.append(
                CatalogAsset(
                    str(episode.get("id") or url),
                    url,
                    title,
                    "mp3",
                    episode.get("mediaType") or "audio/mpeg",
                    size=episode.get("size"),
                    title=title,
                    published=published,
                    track=index,
                )
            )
        return CatalogItem(
            item.id,
            item.source,
            item.media_kind,
            item.title,
            author=item.author,
            language=item.language,
            description=item.description,
            cover_url=item.cover_url,
            assets=tuple(assets),
        )
