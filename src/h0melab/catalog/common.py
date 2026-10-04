"""Shared catalog models, parsers and guarded HTTP helpers."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import niquests as requests

from ..config import DEFAULT_USER_AGENT

USER_AGENT = DEFAULT_USER_AGENT
TIMEOUT = 25


class CatalogError(RuntimeError):
    pass


@dataclass(frozen=True)
class CatalogAsset:
    id: str
    url: str
    label: str
    extension: str
    content_type: str = ""
    size: int | None = None
    title: str = ""
    published: str = ""
    track: int | None = None

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class CatalogItem:
    id: str
    source: str
    media_kind: str
    title: str
    author: str = ""
    year: str = ""
    language: str = ""
    description: str = ""
    cover_url: str = ""
    assets: tuple[CatalogAsset, ...] = field(default_factory=tuple)

    def as_dict(self):
        data = asdict(self)
        data["description"] = html_to_text(self.description)
        data["assets"] = [asset.as_dict() for asset in self.assets]
        return data


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(value: object) -> str:
    """Turn source descriptions into readable text before they reach the UI/OPF."""
    parser = _TextParser()
    parser.feed(str(value or ""))
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


class LinkParser(HTMLParser):
    """Small dependency-free extractor for links, images and audio sources."""

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links = []
        self.images = []
        self.metadata = {}
        self.title = ""
        self.headings = []
        self._anchor = None
        self._text = []
        self._capture = None
        self._capture_text = []

    def _finish_anchor(self):
        if not self._anchor:
            return
        text = " ".join("".join(self._text).split())
        self.links.append((self._anchor, text))
        self._anchor = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content", "").strip()
            if key and content:
                self.metadata[key] = content
                if key in ("og:image", "twitter:image"):
                    self.images.insert(0, urljoin(self.base_url, content))
        elif tag in ("title", "h1", "h2"):
            self._capture = tag
            self._capture_text = []
        if tag == "a" and values.get("href"):
            # Several supported legacy catalogs omit closing </a> tags.  Flush
            # the previous link before a new anchor so its download is not lost.
            self._finish_anchor()
            self._anchor = urljoin(self.base_url, values["href"])
            self._text = []
        elif tag in ("audio", "source") and values.get("src"):
            self.links.append(
                (urljoin(self.base_url, values["src"]), values.get("title", "Audio"))
            )
        elif tag == "img" and values.get("src"):
            self.images.append(urljoin(self.base_url, values["src"]))

    def handle_data(self, data):
        if self._anchor:
            self._text.append(data)
        if self._capture:
            self._capture_text.append(data)

    def handle_endtag(self, tag):
        if tag in {"a", "td"} and self._anchor:
            self._finish_anchor()
        if tag == self._capture:
            text = " ".join("".join(self._capture_text).split())
            if tag == "title":
                self.title = text
            elif text:
                self.headings.append(text)
            self._capture = None
            self._capture_text = []


def ensure_public_url(url: str, *, allowed_hosts: tuple[str, ...] = ()) -> str:
    """Reject local/ambiguous targets before any server-side request."""
    parsed = urlparse(str(url))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise CatalogError("Only public HTTPS URLs are accepted")
    host = parsed.hostname.rstrip(".").lower()
    if allowed_hosts and not any(
        host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts
    ):
        raise CatalogError("URL does not belong to this catalog source")
    try:
        addresses = {
            row[4][0] for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        }
    except OSError as exc:
        raise CatalogError("Catalog host could not be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise CatalogError("Catalog URL resolved to a non-public address")
    return url


def get(url: str, *, allowed_hosts: tuple[str, ...] = (), accept: str = "text/html"):
    def request_once(target):
        try:
            return requests.get(
                target,
                headers={"User-Agent": USER_AGENT, "Accept": accept},
                timeout=TIMEOUT,
                allow_redirects=False,
            )
        except Exception as exc:
            raise CatalogError("Catalog request failed") from exc

    ensure_public_url(url, allowed_hosts=allowed_hosts)
    response = request_once(url)
    redirects = 0
    while response.status_code in (301, 302, 303, 307, 308):
        redirects += 1
        if redirects > 5:
            raise CatalogError("Too many redirects")
        next_url = urljoin(url, response.headers.get("location", ""))
        ensure_public_url(next_url, allowed_hosts=allowed_hosts)
        response.close()
        url = next_url
        response = request_once(url)
    if response.status_code >= 400:
        status = response.status_code
        response.close()
        raise CatalogError(f"Catalog returned HTTP {status}")
    return response


def get_json(url: str, *, allowed_hosts: tuple[str, ...] = ()):
    response = get(url, allowed_hosts=allowed_hosts, accept="application/json")
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise CatalogError("Catalog returned invalid JSON") from exc


def extension_from_url(url: str) -> str:
    name = urlparse(url).path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


class CatalogSource:
    key = ""
    label = ""
    media_kind = ""
    hosts: tuple[str, ...] = ()

    def summary(self):
        return {"key": self.key, "label": self.label, "media_kind": self.media_kind}

    def validate_id(self, item_id: str) -> str:
        return ensure_public_url(item_id, allowed_hosts=self.hosts)

    def search(self, query: str, *, page: int = 1) -> list[CatalogItem]:
        raise NotImplementedError

    def browse(self, *, page: int = 1) -> list[CatalogItem]:
        """Return the source's normal discovery listing."""
        return self.search("", page=page)

    def details(self, item_id: str) -> CatalogItem:
        raise NotImplementedError
