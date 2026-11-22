"""Broadcaster episodes implement the existing queue and library contracts."""

import hashlib
import os
from functools import cached_property
from pathlib import Path

from ...config import NAMING_TEMPLATE, Audio, Subtitles
from ..common.common import ProviderData, check_downloaded
from ..common.library_layout import safe_component
from .client import BroadcasterError
from .dmax import DMAX_PATTERN, DMAXClient
from .zdf import ZDF_PATTERN, ZDFClient

LANGUAGES = {"German Dub": ("deu", Audio.GERMAN), "English Dub": ("eng", Audio.ENGLISH)}


class BroadcasterEpisode:
    def __init__(
        self,
        url,
        selected_path=None,
        selected_language=None,
        selected_provider=None,
        selected_subtitle_language="none",
    ):
        if not isinstance(url, str) or not self.pattern.fullmatch(url):
            raise BroadcasterError(f"Invalid {self.name} episode URL")
        self.url = url
        self.selected_path = (
            selected_path
            or os.getenv("H0MELAB_DOWNLOAD_PATH")
            or str(Path.home() / "Downloads")
        )
        self.selected_language = selected_language or "German Dub"
        if self.selected_language not in LANGUAGES:
            raise BroadcasterError(f"{self.name}: unsupported audio language")
        if selected_subtitle_language not in ("none", "deu"):
            raise BroadcasterError(f"{self.name}: unsupported subtitle language")
        self.selected_provider = self.name
        self.selected_subtitle_language = selected_subtitle_language

    def _fetch(self):
        client = self.client_cls()
        try:
            return client.extract(self.url)
        finally:
            client.close()

    @cached_property
    def video(self):
        return self._fetch()

    @property
    def title(self):
        return self.video.title

    title_de = title
    title_en = title

    @property
    def title_cleaned(self):
        return safe_component(self.title, self.video.id)

    @property
    def description(self):
        return self.video.description

    @property
    def poster_url(self):
        return self.video.poster

    @property
    def release_year(self):
        return self.video.date[:4] if self.video.date[:4].isdigit() else ""

    @property
    def season_number(self):
        return self.video.season

    @property
    def episode_number(self):
        return self.video.episode

    @property
    def is_movie(self):
        return self.video.season is None and self.video.episode is None

    @property
    def video_identity(self):
        video = self.video
        if self.is_movie:
            return {
                "media_type": "movie",
                "title": video.title,
                "year": self.release_year,
                "id": f"{self.name.lower()}:{video.id}",
            }
        if (
            video.season is None
            or video.episode is None
            or video.season < 0
            or video.episode < 1
            or not video.series
        ):
            raise BroadcasterError(
                f"{self.name}: source did not confirm series/season/episode identity"
            )
        return {
            "media_type": "series",
            "title": video.series,
            "year": "",
            "season": video.season,
            "episode": video.episode,
            "end": video.episode,
        }

    @property
    def provider_data(self):
        return ProviderData(
            {
                (audio, Subtitles.NONE): {self.name: self.url}
                for code, audio in LANGUAGES.values()
                if any(
                    f.language == code and f.role == "main" for f in self.video.formats
                )
            }
        )

    def selected_format(self):
        if self.selected_language not in LANGUAGES:
            raise BroadcasterError(f"{self.name}: unsupported audio language")
        code = LANGUAGES[self.selected_language][0]
        formats = [
            f for f in self.video.formats if f.language == code and f.role == "main"
        ]
        if not formats:
            raise BroadcasterError(
                f"{self.name}: requested standard audio is not available"
            )
        # Adaptive HLS selects its best rendition in the native HLS engine.
        # DASH stays a last resort; direct MP4 gives a reliable language-specific file.
        return max(
            formats,
            key=lambda f: (
                f.protocol == "hls",
                f.height,
                f.bitrate,
                f.protocol == "http",
            ),
        )

    def selected_subtitle_track(self):
        return next(
            (
                t
                for t in self.video.subtitles
                if t.language == self.selected_subtitle_language
            ),
            None,
        )

    @property
    def _episode_path(self):
        from ...web.media_files import episode_path, routed_episode_path

        if (routed := routed_episode_path(self)) is not None:
            return routed
        root = Path(self.selected_path).expanduser().absolute()
        template = os.getenv("H0MELAB_NAMING_TEMPLATE", NAMING_TEMPLATE)
        extension = Path(template).suffix.lower()
        if extension not in (".mkv", ".mp4"):
            extension = ".mkv"
        if self.is_movie:
            # Stable source ID avoids collisions between equally named clips.
            content_key = hashlib.sha256(self.video.id.encode()).hexdigest()[:10]
            stem = (
                f"{safe_component(self.title, 'Video', max_length=70)} "
                f"[{self.name}-{content_key}] [{safe_component(self.selected_language, 'Audio')}]"
            )
            path = root / stem / (stem + extension)
        else:
            identity = self.video_identity
            fields = {
                "title": safe_component(
                    identity["title"], self.title_cleaned, max_length=70
                ),
                "year": "",
                "imdbid": "",
                "season": f"{identity['season']:02d}",
                "episode": f"{identity['episode']:03d}",
                "language": self.selected_language,
                "resolution": getattr(self, "_resolution", "unknown"),
            }
            for key in fields:
                template = template.replace(f"%{key}%", "{" + key + "}")
            path = root.joinpath(
                *(
                    safe_component(
                        part.format(**fields)
                        .replace("()", "")
                        .replace("[imdbid-]", ""),
                        "Video",
                    )
                    for part in template.replace("\\", "/").split("/")
                )
            )
            path = path.with_suffix(extension)
        return episode_path(self, path)

    @property
    def _file_name(self):
        return self._episode_path.stem

    @property
    def _folder_path(self):
        return self._episode_path.parent

    @property
    def is_downloaded(self):
        return check_downloaded(self._episode_path)

    def download(self):
        from .transfer import download_broadcast

        # Playback URLs are short-lived: never persist them in the queue.
        previous = self.video_identity if "video" in self.__dict__ else None
        self.__dict__.pop("video", None)
        if previous is not None and self.video_identity != previous:
            raise BroadcasterError(
                f"{self.name}: video identity changed; reopen the selection"
            )
        return download_broadcast(self)


class ZDFEpisode(BroadcasterEpisode):
    name = "ZDF"
    pattern = ZDF_PATTERN
    client_cls = ZDFClient


class DMAXEpisode(BroadcasterEpisode):
    name = "DMAX"
    pattern = DMAX_PATTERN
    client_cls = DMAXClient
