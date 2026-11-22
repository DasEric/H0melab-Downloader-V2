"""ZDF GraphQL/PTMD extraction adapted to H0melab's native transfer model.

Protocol reference: https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/zdf.py
"""

import json
import re
import time
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

from ..common.subtitles import SubtitleTrack
from .client import (
    BroadcasterError,
    JSONClient,
    MediaFormat,
    Video,
    language,
    media_url,
    number,
)

ZDF_PATTERN = re.compile(
    r"https?://(?:www\.)?zdf\.de/(?:(?:video|play)/[^?#]+|[^?#]+\.html)/?(?:\?[^#]*)?(?:#.*)?$"
)

VIDEO_QUERY = """
query VideoByCanonical($canonical: String!) {
  videoByCanonical(canonical: $canonical) {
    canonical title leadParagraph editorialDate
    teaser { description image { list } }
    episodeInfo { episodeNumber seasonNumber }
    smartCollection { canonical title }
    currentMedia { nodes {
      ptmdTemplate
      ... on VodMedia { duration vodMediaType }
      ... on LiveMedia { liveMediaType }
    } }
  }
}
"""


class ZDFClient(JSONClient):
    name = "ZDF"

    def search(self, keyword):
        """Use the public HbbTV search and resolve canonical URLs via GraphQL."""
        data = self.request(
            "https://hbbtv.zdf.de/legacy-al/search",
            params={
                "t": keyword,
                "abGroup": "gruppe-c",
                "abName": "ab-" + datetime.now(UTC).date().isoformat(),
            },
        )
        if not isinstance(data.get("result"), list):
            raise BroadcasterError("ZDF: invalid search response")
        candidates, seen = [], set()
        for item in data["result"]:
            link = item.get("link") or {}
            canonical = link.get("internalId")
            kind = item.get("__typename") or ""
            if (
                not isinstance(canonical, str)
                or not canonical
                or canonical in seen
                or link.get("live")
                or (kind != "Video" and not kind.endswith("SmartCollection"))
            ):
                continue
            seen.add(canonical)
            candidates.append((canonical, kind == "Video", media_url(item.get("img"))))
        results = []
        for offset in range(0, len(candidates), 20):
            batch = candidates[offset : offset + 20]
            fields = []
            for index, (canonical, is_video, _) in enumerate(batch):
                field = "videoByCanonical" if is_video else "smartCollectionByCanonical"
                extra = "episodeInfo { seasonNumber episodeNumber }" if is_video else ""
                fields.append(
                    f"r{index}: {field}(canonical: {json.dumps(canonical)}) {{ title sharingUrl {extra} }}"
                )
            response = self.request(
                "https://api.zdf.de/graphql",
                method="POST",
                headers={**self.headers(), "Apollo-Require-Preflight": "true"},
                json={"query": "query { " + " ".join(fields) + " }"},
            )
            if response.get("errors") or not isinstance(response.get("data"), dict):
                raise BroadcasterError("ZDF: search URL resolution failed")
            for index, (_, is_video, poster) in enumerate(batch):
                row = response["data"].get(f"r{index}") or {}
                url = row.get("sharingUrl") or ""
                if not row.get("title") or urlparse(url).hostname not in (
                    "zdf.de",
                    "www.zdf.de",
                ):
                    continue
                info = row.get("episodeInfo") or {}
                results.append(
                    {
                        "title": row["title"],
                        "url": url,
                        "poster": poster,
                        "source": "zdf",
                        "media_type": "movie"
                        if is_video and info.get("episodeNumber") is None
                        else "series",
                    }
                )
        return results

    def __init__(self, session=None):
        super().__init__(session)
        self._token = {}

    def headers(self):
        if (number(self._token.get("expires")) or 0) <= time.time() + 30:
            self._token = self.request(
                "https://zdf-prod-futura.zdf.de/mediathekV2/token"
            )
        if not self._token.get("token") or not self._token.get("type"):
            raise BroadcasterError("ZDF: API did not return a token")
        return {"Api-Auth": f"{self._token['type']} {self._token['token']}"}

    def extract(self, url):
        if not ZDF_PATTERN.fullmatch(url):
            raise BroadcasterError(
                "ZDF: enter a video/play URL or a legacy .html video URL"
            )
        canonical = urlparse(url).path.rstrip("/").split("/")[-1].removesuffix(".html")
        data = self.request(
            "https://api.zdf.de/graphql",
            method="POST",
            headers={**self.headers(), "Apollo-Require-Preflight": "true"},
            json={
                "operationName": "VideoByCanonical",
                "query": VIDEO_QUERY,
                "variables": {"canonical": canonical},
            },
        )
        if data.get("errors"):
            raise BroadcasterError(
                "ZDF: metadata query failed; the API may have changed"
            )
        source = (data.get("data") or {}).get("videoByCanonical")
        if not source:
            raise BroadcasterError("ZDF: no playable video found at this URL")
        info = source.get("episodeInfo") or {}
        teaser = source.get("teaser") or {}
        pictures = (teaser.get("image") or {}).get("list") or {}
        video = Video(
            id=source.get("canonical") or canonical,
            title=source.get("title") or canonical,
            description=source.get("leadParagraph") or teaser.get("description") or "",
            poster=media_url(pictures.get("original"))
            or next((media_url(v) for v in pictures.values() if media_url(v)), ""),
            series=(source.get("smartCollection") or {}).get("title") or "",
            season=number(info.get("seasonNumber")),
            episode=number(info.get("episodeNumber")),
            date=source.get("editorialDate") or "",
        )
        seen = set()
        for node in (source.get("currentMedia") or {}).get("nodes") or []:
            if "liveMediaType" in node:
                raise BroadcasterError(
                    "ZDF: live recordings are not supported; select an on-demand video"
                )
            template = node.get("ptmdTemplate")
            if not template:
                continue
            ptmd_url = urljoin(
                "https://api.zdf.de", template.replace("{playerId}", "android_native_6")
            )
            if urlparse(ptmd_url).hostname != "api.zdf.de":
                raise BroadcasterError("ZDF: unexpected playback metadata host")
            ptmd = self.request(ptmd_url, headers=self.headers())
            duration = ((ptmd.get("attributes") or {}).get("duration") or {}).get(
                "value"
            )
            video.duration = (number(duration) or 0) / 1000 or video.duration
            for caption in ptmd.get("captions") or []:
                caption_url = media_url(caption.get("uri"))
                if caption_url and caption_url not in seen:
                    seen.add(caption_url)
                    ext = urlparse(caption_url).path.rsplit(".", 1)[-1].lower()
                    if ext in ("vtt", "srt"):
                        video.subtitles.append(
                            SubtitleTrack(
                                caption_url,
                                language(caption.get("language")),
                                format=ext,
                            )
                        )
            for priority in ptmd.get("priorityList") or []:
                for stream in priority.get("formitaeten") or []:
                    if stream.get("drm"):
                        continue
                    for quality in stream.get("qualities") or []:
                        for track in (quality.get("audio") or {}).get("tracks") or []:
                            link = media_url(track.get("uri"))
                            if not link or link in seen:
                                continue
                            ext = urlparse(link).path.rsplit(".", 1)[-1].lower()
                            protocol = {
                                "m3u8": "hls",
                                "mpd": "dash",
                                "mp4": "http",
                            }.get(ext)
                            if not protocol:
                                continue
                            seen.add(link)
                            role = (
                                "dgs"
                                if node.get("vodMediaType") == "DGS"
                                else track.get("class") or "main"
                            )
                            size = re.search(r"(\d+)x(\d+)", link)
                            rate = re.search(r"_(\d+)k_", link)
                            video.formats.append(
                                MediaFormat(
                                    link,
                                    protocol,
                                    language(track.get("language")),
                                    role,
                                    int(size[2])
                                    if size
                                    else {"hd": 720, "veryhigh": 480, "high": 360}.get(
                                        quality.get("quality"), 0
                                    ),
                                    int(rate[1]) if rate else 0,
                                )
                            )
        if not video.formats:
            raise BroadcasterError(
                "ZDF: no supported on-demand media formats are available"
            )
        return video
