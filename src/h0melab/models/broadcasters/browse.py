"""Bounded public catalogue previews, reusing existing card/open-title flows."""

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
import niquests
from .client import BroadcasterError, media_url
from .dmax import DMAXClient, DMAX_COLLECTION_PATTERN


class ZDFCards(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []
        self.card = None
        self.heading = False
        self.seen = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.card = {
                "url": urljoin("https://www.zdf.de/", attrs.get("href", "")),
                "title": [],
                "poster": "",
            }
        elif self.card is not None:
            if tag in ("h2", "h3", "h4"):
                self.heading = True
            elif tag == "img" and not self.card["poster"]:
                self.card["poster"] = media_url(attrs.get("src"))

    def handle_data(self, data):
        if self.card is not None and self.heading:
            self.card["title"].append(data.strip())

    def handle_endtag(self, tag):
        if tag in ("h2", "h3", "h4"):
            self.heading = False
        if tag == "a" and self.card is not None:
            row = self.card
            self.card = None
            self.heading = False
            parsed = urlsplit(row["url"])
            title = " ".join(filter(None, row["title"]))
            # Category/navigation/live tiles are not downloadable titles.
            if (
                parsed.hostname in ("zdf.de", "www.zdf.de")
                and parsed.scheme == "https"
                and parsed.path.rstrip("/").endswith("-100")
                and not parsed.fragment
                and not any(
                    word in parsed.path for word in ("livestream", "liveticker")
                )
                and title
                and row["poster"]
                and row["url"] not in self.seen
            ):
                self.seen.add(row["url"])
                self.rows.append(
                    {
                        "url": row["url"],
                        "title": title,
                        "poster_url": row["poster"],
                        "source": "zdf",
                    }
                )


def fetch_zdf_preview():
    response = niquests.get("https://www.zdf.de/", timeout=15)
    response.raise_for_status()
    parser = ZDFCards()
    parser.feed(response.text)
    if not parser.rows:
        raise BroadcasterError("ZDF preview catalogue unavailable")
    return parser.rows[:24]


def fetch_dmax_preview():
    data = DMAXClient().site(
        "search/page/", q="", **{"page[number]": 1, "page[size]": 24}
    )
    if not isinstance(data.get("data"), list):
        raise BroadcasterError("DMAX preview catalogue unavailable")
    rows, seen = [], set()
    for item in data["data"]:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        slug = item.get("slug") or ""
        parent = item.get("parentSlug") or ""
        url = "https://dmax.de/" + (parent + "/" if parent else "") + slug
        poster = media_url((item.get("image") or {}).get("url"))
        if (
            slug
            and poster
            and DMAX_COLLECTION_PATTERN.fullmatch(url)
            and url not in seen
        ):
            seen.add(url)
            rows.append(
                {
                    "url": url,
                    "title": item["title"],
                    "poster_url": poster,
                    "source": "dmax",
                    "media_type": "series",
                }
            )
    if not rows:
        raise BroadcasterError("DMAX preview catalogue unavailable")
    return rows[:24]
