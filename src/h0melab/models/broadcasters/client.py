"""Small, provider-scoped JSON clients and normalized media descriptions."""

from dataclasses import dataclass, field
from urllib.parse import urlparse

import niquests


class BroadcasterError(ValueError):
    """An actionable source error, without tokens or signed playback URLs."""


def media_url(value):
    if not isinstance(value, str):
        return ""
    parsed = urlparse(value)
    return value if parsed.scheme in ("https", "http") and parsed.hostname else ""


def number(value):
    try:
        return int(value) if value is not None and not isinstance(value, bool) else None
    except (ValueError, TypeError):
        return None


def language(value):
    value = str(value or "deu").lower().split("-")[0]
    return {"de": "deu", "ger": "deu", "en": "eng", "ja": "jpn"}.get(value, value)


@dataclass(frozen=True)
class MediaFormat:
    url: str
    protocol: str
    language: str = "deu"
    role: str = "main"
    height: int = 0
    bitrate: int = 0


@dataclass
class Video:
    id: str
    title: str
    description: str = ""
    poster: str = ""
    series: str = ""
    season: int | None = None
    episode: int | None = None
    duration: float = 0
    date: str = ""
    formats: list[MediaFormat] = field(default_factory=list)
    subtitles: list = field(default_factory=list)


class JSONClient:
    name = "Broadcaster"

    def __init__(self, session=None):
        # Do not share broadcaster credentials with the global hoster session.
        self.session = session or niquests.Session()
        self.session.headers.update(
            {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        )

    def request(self, url, *, method="GET", **kwargs):
        try:
            response = self.session.request(method, url, timeout=25, **kwargs)
        except niquests.RequestException:
            raise BroadcasterError(
                f"{self.name}: API connection failed; please retry later"
            ) from None
        if response.status_code in (401, 403):
            code = ""
            try:
                code = str(response.json().get("errors", [{}])[0].get("code", ""))
            except (ValueError, AttributeError, IndexError, TypeError):
                pass
            if "geoblocked" in code:
                raise BroadcasterError(
                    f"{self.name}: this video is not available in your region"
                )
            if "missingpackage" in code:
                raise BroadcasterError(
                    f"{self.name}: this video requires an account entitlement"
                )
            raise BroadcasterError(f"{self.name}: access denied or session expired")
        if response.status_code == 404:
            raise BroadcasterError(f"{self.name}: video is no longer available")
        if response.status_code >= 400:
            raise BroadcasterError(
                f"{self.name}: API returned HTTP {response.status_code}"
            )
        try:
            data = response.json()
        except ValueError:
            raise BroadcasterError(f"{self.name}: API returned invalid JSON") from None
        if not isinstance(data, dict):
            raise BroadcasterError(f"{self.name}: unexpected API response")
        return data

    def close(self):
        self.session.close()
