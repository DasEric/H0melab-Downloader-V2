"""DMAX's Aurora catalogue and playback API, without yt-dlp imports."""

import re
import uuid
from functools import cached_property
from urllib.parse import parse_qs, quote, urlparse

from .client import BroadcasterError, JSONClient, MediaFormat, Video, media_url, number

DMAX_PATTERN = re.compile(
    r"https?://(?:www\.)?dmax\.de/(?:(?:programme|show|sendungen)/)?"
    r"(?P<show>(?!(?:programme|show|sendungen)/)[^/?#]+)/"
    r"(?:video/)?(?P<slug>[^/?#]+)/?(?:\?[^#]*)?(?:#.*)?$"
)
DMAX_COLLECTION_PATTERN = re.compile(
    r"https?://(?:www\.)?dmax\.de/(?:(?:sendungen|programme|show)/)?"
    r"[\w-]+/?(?:\?[^#]*)?(?:#.*)?$"
)


class DMAXClient(JSONClient):
    name = "DMAX"
    base = "https://public.aurora.enhanced.live/"

    @cached_property
    def _headers(self):
        token = self.request(
            self.base + "token",
            params={"realm": "de", "deviceId": uuid.uuid4().hex},
        )
        value = ((token.get("data") or {}).get("attributes") or {}).get("token")
        if not value:
            raise BroadcasterError("DMAX: API did not return a session token")
        return {
            "Authorization": f"Bearer {value}",
            "x-disco-params": "realm=de",
            "x-disco-client": "Alps:HyogaPlayer:0.0.0",
            "Referer": "https://dmax.de/",
        }

    def headers(self):
        return self._headers

    def site(self, path, **params):
        return self.request(
            self.base + "site/" + path,
            params={
                "include": "default",
                "filter[environment]": "dmaxde",
                "v": "2",
                **params,
            },
        )

    def show(self, url):
        parts = urlparse(url).path.strip("/").split("/")
        slug = parts[1] if parts[0] in ("sendungen", "programme", "show") else parts[0]
        data = self.site("shows/" + quote(slug, safe="") + "/")
        show_id = str((data.get("attributes") or {}).get("showId") or "")
        if not show_id.isdigit() or not data.get("title"):
            raise BroadcasterError("DMAX: page is not an available show")
        return data, show_id, slug

    def videos(self, show_id):
        rows, seen = [], set()
        for page in range(1, 1001):
            data = self.request(
                self.base + "content/videos",
                headers=self.headers(),
                params={
                    "filter[show.id]": show_id,
                    "page[size]": 100,
                    "page[number]": page,
                },
            )
            items = data.get("data")
            total = number((data.get("meta") or {}).get("totalPages"))
            if not isinstance(items, list) or total is None or total < 0:
                raise BroadcasterError("DMAX: invalid catalogue pagination")
            fresh = [item for item in items if str(item.get("id")) not in seen]
            if items and not fresh:
                raise BroadcasterError("DMAX: catalogue pagination did not advance")
            for item in fresh:
                linked_show = (
                    ((item.get("relationships") or {}).get("show") or {}).get("data")
                    or {}
                ).get("id")
                if linked_show is not None and str(linked_show) != str(show_id):
                    raise BroadcasterError("DMAX: catalogue returned a different show")
                if not item.get("id") or not (item.get("attributes") or {}).get(
                    "alternateId"
                ):
                    raise BroadcasterError("DMAX: video identity is missing")
                seen.add(str(item["id"]))
                rows.append(item)
                if len(rows) > 20000:
                    raise BroadcasterError("DMAX: catalogue exceeds 20000 videos")
            if page >= total:
                return rows
            if not items:
                raise BroadcasterError("DMAX: incomplete catalogue page")
        raise BroadcasterError("DMAX: catalogue pagination limit reached")

    def search(self, keyword):
        results, seen = [], set()
        page = 1
        for _ in range(1000):
            data = self.site(
                "search/page/", q=keyword, **{"page[number]": page, "page[size]": 20}
            )
            if not isinstance(data.get("data"), list):
                raise BroadcasterError("DMAX: invalid search response")
            for item in data["data"]:
                if item.get("type") != "showpage" or not item.get("title"):
                    continue
                slug = item.get("slug") or ""
                parent = item.get("parentSlug") or ""
                url = "https://dmax.de/" + (parent + "/" if parent else "") + slug
                if DMAX_COLLECTION_PATTERN.fullmatch(url) and url not in seen:
                    seen.add(url)
                    results.append(
                        {
                            "title": item["title"],
                            "url": url,
                            "poster": media_url((item.get("image") or {}).get("url")),
                            "source": "dmax",
                            "media_type": "series",
                        }
                    )
            next_page = (data.get("meta") or {}).get("nextPage")
            if next_page is None:
                return results
            next_page = number(next_page)
            if next_page is None or next_page <= page:
                raise BroadcasterError("DMAX: search pagination did not advance")
            page = next_page
        raise BroadcasterError("DMAX: search pagination limit reached")

    def extract(self, url):
        match = DMAX_PATTERN.fullmatch(url)
        if not match:
            raise BroadcasterError("DMAX: enter an individual episode URL")
        _, show_id, _ = self.show(url)
        slug = match.group("slug")
        matches = [
            item
            for item in self.videos(show_id)
            if (item.get("attributes") or {}).get("alternateId") == slug
        ]
        if len(matches) != 1:
            raise BroadcasterError(
                "DMAX: episode is unavailable; select its current URL from the show overview"
            )
        video_id = str(matches[0]["id"])
        headers = self.headers()
        data = self.request(
            self.base + "content/videos/" + quote(video_id, safe="/"),
            headers=headers,
            params={"include": "images,primaryChannel,show,tags"},
        )
        item = data.get("data") or {}
        attributes = item.get("attributes") or {}
        if attributes.get("drmEnabled") or attributes.get("videoType") == "LIVE":
            raise BroadcasterError("DMAX: protected or live playback is not supported")
        if not item.get("id") or not attributes.get("name"):
            raise BroadcasterError("DMAX: incomplete video metadata")
        included = data.get("included") or []
        shows = [
            row.get("attributes") or {} for row in included if row.get("type") == "show"
        ]
        images = [
            row.get("attributes") or {}
            for row in included
            if row.get("type") == "image"
        ]
        images.sort(key=lambda image: number(image.get("width")) or 0, reverse=True)
        video = Video(
            id=str(item["id"]),
            title=attributes["name"].strip(),
            description=attributes.get("description") or "",
            poster=next(
                (media_url(i.get("src")) for i in images if media_url(i.get("src"))), ""
            ),
            series=shows[0].get("name", "") if shows else "",
            season=number(attributes.get("seasonNumber")),
            episode=number(attributes.get("episodeNumber")),
            duration=(number(attributes.get("videoDuration")) or 0) / 1000,
            date=attributes.get("publishStart") or "",
        )
        playback = self.request(
            self.base + "playback/v3/videoPlaybackInfo",
            method="POST",
            headers=headers,
            json={
                "deviceInfo": {"adBlocker": False, "drmSupported": False},
                "videoId": video.id,
                "wisteriaProperties": {},
            },
        )
        streams = ((playback.get("data") or {}).get("attributes") or {}).get(
            "streaming"
        ) or []
        if isinstance(streams, dict):
            streams = [
                {**value, "type": key}
                for key, value in streams.items()
                if isinstance(value, dict)
            ]
        for stream in streams:
            link = media_url(stream.get("url"))
            protection = stream.get("protection") or {}
            if (
                link
                and not stream.get("drm")
                and not protection.get("drmEnabled")
                and not protection.get("clearkeyEnabled")
                and stream.get("type") in ("hls", "dash")
            ):
                video.formats.append(MediaFormat(link, stream["type"]))
        if not video.formats:
            raise BroadcasterError(
                "DMAX: no supported unprotected playback formats are available"
            )
        return video


def dmax_title(url, **kwargs):
    from .episode import DMAXEpisode

    return (
        DMAXEpisode(url, **kwargs)
        if DMAX_PATTERN.fullmatch(url)
        else DMAXCollection(url)
    )


class DMAXCollection:
    is_movie = False

    def __init__(self, url):
        if not DMAX_COLLECTION_PATTERN.fullmatch(url):
            raise BroadcasterError("DMAX: invalid show URL")
        self.url = url
        values = parse_qs(urlparse(url).query).get("staffel", [])
        self.requested_season = number(values[-1]) if values else None
        if values and (self.requested_season is None or self.requested_season < 0):
            raise BroadcasterError("DMAX: invalid requested season")

    @cached_property
    def data(self):
        client = DMAXClient()
        try:
            data, show_id, slug = client.show(self.url)
            self.canonical_url = "https://dmax.de/sendungen/" + slug
            self.items = client.videos(show_id)
            return data
        finally:
            client.close()

    @property
    def title(self):
        return self.data["title"]

    @property
    def description(self):
        return self.data.get("description") or self.data.get("metaDescription") or ""

    @property
    def poster_url(self):
        return next(
            (
                media_url((row.get("media") or {}).get("url"))
                for row in self.data.get("metaMedia", [])
                if row.get("role") == "default"
            ),
            "",
        )

    def episode_rows(self):
        _ = self.data
        rows = []
        for item in self.items:
            a = item["attributes"]
            season = number(a.get("seasonNumber"))
            if self.requested_season is not None and season != self.requested_season:
                continue
            slug = a["alternateId"]
            if not re.fullmatch(r"[\w-]+", slug):
                raise BroadcasterError("DMAX: invalid episode slug")
            rows.append(
                {
                    "url": self.canonical_url + "/" + slug,
                    "season_number": season,
                    "episode_number": number(a.get("episodeNumber")),
                    "title_de": a.get("name") or "",
                    "title_en": "",
                    "downloaded": False,
                    "available_languages": [],
                }
            )
        if not rows:
            raise BroadcasterError(
                "DMAX: no available episodes in the requested show or season"
            )
        return sorted(
            rows,
            key=lambda row: (row["season_number"] or 0, row["episode_number"] or 0),
        )

    def season_rows(self):
        rows = self.episode_rows()
        seasons = sorted(
            {r["season_number"] for r in rows if r["season_number"] is not None}
        )
        result = [
            {
                "url": self.canonical_url + f"?staffel={season}",
                "season_number": season,
                "episode_count": sum(r["season_number"] == season for r in rows),
                "are_movies": False,
                "label": "",
            }
            for season in seasons
        ]
        result.extend(
            {
                "url": r["url"],
                "season_number": None,
                "episode_count": 1,
                "are_movies": True,
                "label": r["title_de"],
            }
            for r in rows
            if r["season_number"] is None
        )
        return result
