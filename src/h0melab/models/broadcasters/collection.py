"""ZDF collections: lazy season loading, cursor pagination and source identities."""

import json
import re
from functools import cached_property
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import niquests

from .client import BroadcasterError, media_url, number
from .episode import ZDFEpisode
from .zdf import ZDF_PATTERN, ZDFClient

ZDF_COLLECTION_PATTERN = re.compile(
    r"https?://(?:www\.)?zdf\.de/(?!video/|play/)"
    r"(?![^?#]*\.html/?(?:[?#]|$))[^?#]+/?(?:\?[^#]*)?(?:#.*)?$"
)
COLLECTION_HASH = "cb49420e133bd668ad895a8cea0e65cba6aa11ac1cacb02341ff5cf32a17cd02"
SEASON_HASH = "9412a0f4ac55dc37d46975d461ec64bfd14380d815df843a1492348f77b5c99a"
MAX_EPISODES = 20000


def is_zdf_collection(url):
    return isinstance(url, str) and bool(ZDF_COLLECTION_PATTERN.fullmatch(url))


def zdf_title(url, **kwargs):
    return ZDFCollection(url) if is_zdf_collection(url) else ZDFEpisode(url, **kwargs)


class ZDFCollectionClient(ZDFClient):
    def resolve_url(self, url):
        """Resolve legacy show slugs without sending API credentials to webpages."""
        try:
            response = self.session.get(url, timeout=25)
            response.raise_for_status()
        except niquests.RequestException:
            raise BroadcasterError("ZDF: collection page could not be loaded") from None
        resolved = str(response.url)
        if not is_zdf_collection(resolved):
            raise BroadcasterError("ZDF: collection redirected to an unexpected URL")
        return resolved

    def collection_query(self, operation, variables, digest):
        data = self.request(
            "https://api.zdf.de/graphql",
            headers={**self.headers(), "Apollo-Require-Preflight": "true"},
            params={
                "operationName": operation,
                "variables": json.dumps(variables),
                "extensions": json.dumps(
                    {"persistedQuery": {"version": 1, "sha256Hash": digest}}
                ),
            },
        )
        result = (data.get("data") or {}).get("smartCollectionByCanonical")
        if data.get("errors") or not isinstance(result, dict):
            raise BroadcasterError(
                "ZDF: collection is unavailable or its API has changed"
            )
        return result

    def collection(self, canonical):
        return self.collection_query(
            "GetSmartCollectionByCanonical",
            {
                "canonical": canonical,
                "videoPageSize": 100,
            },
            COLLECTION_HASH,
        )

    def season_page(self, canonical, index, cursor=None):
        variables = {
            "canonical": canonical,
            "seasonIndex": index,
            "episodesPageSize": 24,
        }
        if cursor:
            variables["episodesAfter"] = cursor
        return self.collection_query("seasonByCanonical", variables, SEASON_HASH)


class ZDFCollection:
    """A show overview is not a download URL; queue entries always use videos."""

    def __init__(self, url):
        if not is_zdf_collection(url):
            raise BroadcasterError("ZDF: invalid collection URL")
        self.url = url
        values = parse_qs(urlparse(url).query).get("staffel", [])
        self.requested_season = number(values[-1]) if values else None
        if values and (self.requested_season is None or self.requested_season < 0):
            raise BroadcasterError("ZDF: invalid requested season")

    @cached_property
    def data(self):
        client = ZDFCollectionClient()
        try:
            resolved = client.resolve_url(self.url)
            self.canonical = urlparse(resolved).path.rstrip("/").rsplit("/", 1)[-1]
            self.canonical_url = urlunparse(
                urlparse(resolved)._replace(query="", fragment="")
            )
            data = client.collection(self.canonical)
            if not data.get("title"):
                raise BroadcasterError("ZDF: collection metadata has no title")
            return data
        finally:
            client.close()

    @property
    def title(self):
        return self.data["title"]

    @property
    def description(self):
        return self.data.get("infoText") or ""

    @property
    def poster_url(self):
        image = self.data.get("imageWithoutLogo") or {}
        layouts = image.get("layouts") or image.get("list") or {}
        return media_url(layouts.get("original")) or next(
            (media_url(v) for v in layouts.values() if media_url(v)), ""
        )

    @property
    def is_movie(self):
        return not self._seasons and bool(self._single_video_url)

    @property
    def _single_video_url(self):
        url = (self.data.get("video") or {}).get("sharingUrl") or ""
        return url if ZDF_PATTERN.fullmatch(url) else ""

    @property
    def _seasons(self):
        rows = (self.data.get("seasons") or {}).get("seasons") or []
        result = []
        seen = set()
        for index, row in enumerate(rows):
            season_number = number(row.get("number"))
            if season_number is None or season_number < 0 or season_number in seen:
                raise BroadcasterError("ZDF: invalid or duplicate season identity")
            seen.add(season_number)
            result.append((index, season_number, row))
        if self.requested_season is not None and self.requested_season not in seen:
            raise BroadcasterError("ZDF: requested season is not available")
        return result

    def season_rows(self):
        seasons = self._seasons
        if not seasons:
            if not self._single_video_url:
                raise BroadcasterError("ZDF: collection contains no selectable videos")
            return [
                {
                    "url": self._single_video_url,
                    "season_number": None,
                    "episode_count": 1,
                    "are_movies": True,
                    "label": "Video",
                }
            ]
        return [
            {
                "url": self.canonical_url + "?" + urlencode({"staffel": season_number}),
                "season_number": season_number,
                "episode_count": number(row.get("countEpisodes")),
                "are_movies": False,
                "label": row.get("title") or "",
            }
            for _, season_number, row in seasons
            if self.requested_season is None or season_number == self.requested_season
        ]

    def episode_rows(self):
        seasons = self._seasons
        if not seasons:
            raise BroadcasterError("ZDF: open the individual video from the collection")
        rows, seen_urls = [], set()
        client = ZDFCollectionClient()
        try:
            for index, season_number, _ in seasons:
                if (
                    self.requested_season is not None
                    and season_number != self.requested_season
                ):
                    continue
                cursor = None
                seen_cursors = set()
                for _page in range(1000):
                    page = client.season_page(self.canonical, index, cursor)
                    nodes = (page.get("seasons") or {}).get("nodes") or []
                    if (
                        len(nodes) != 1
                        or number(nodes[0].get("number")) != season_number
                    ):
                        raise BroadcasterError(
                            "ZDF: season page returned an unexpected season"
                        )
                    episodes = nodes[0].get("episodes") or {}
                    for item in episodes.get("nodes") or []:
                        url = item.get("sharingUrl") or ""
                        if not ZDF_PATTERN.fullmatch(url):
                            raise BroadcasterError(
                                "ZDF: collection returned an invalid episode URL"
                            )
                        if url in seen_urls:
                            continue
                        seen_urls.add(url)
                        info = item.get("episodeInfo") or {}
                        rows.append(
                            {
                                "url": url,
                                "season_number": number(info.get("seasonNumber")),
                                "episode_number": number(info.get("episodeNumber")),
                                "title_de": item.get("title")
                                or (item.get("teaser") or {}).get("title")
                                or "",
                                "title_en": "",
                                "downloaded": False,
                                "available_languages": [],
                            }
                        )
                        if len(rows) > MAX_EPISODES:
                            raise BroadcasterError(
                                "ZDF: collection exceeds 20000 videos; select one season"
                            )
                    page_info = episodes.get("pageInfo")
                    if (
                        not isinstance(page_info, dict)
                        or "hasNextPage" not in page_info
                    ):
                        raise BroadcasterError(
                            "ZDF: episode pagination information is missing"
                        )
                    if not page_info["hasNextPage"]:
                        break
                    cursor = page_info.get("endCursor")
                    if (
                        not isinstance(cursor, str)
                        or not cursor
                        or cursor in seen_cursors
                    ):
                        raise BroadcasterError(
                            "ZDF: episode pagination did not advance"
                        )
                    seen_cursors.add(cursor)
                else:
                    raise BroadcasterError("ZDF: episode pagination limit reached")
        finally:
            client.close()
        return rows
