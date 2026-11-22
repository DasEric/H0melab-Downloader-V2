from __future__ import annotations

from dataclasses import dataclass
from re import Pattern
from urllib.parse import urlparse, urlunparse

from .config import (
    FILMPALAST_SERIES_PATTERN,
    H0MELAB_EPISODE_PATTERN,
    H0MELAB_SEASON_PATTERN,
    H0MELAB_SERIES_PATTERN,
    HANIME_TV_SERIES_PATTERN,
    MANGA_FIRE_CHAPTER_PATTERN,
    MANGA_FIRE_SERIES_PATTERN,
    MEGAKINO_SERIES_PATTERN,
    MOFLIX_SERIES_PATTERN,
    SERIENSTREAM_EPISODE_PATTERN,
    SERIENSTREAM_SEASON_PATTERN,
    SERIENSTREAM_SERIES_PATTERN,
)
from .models import (
    AniworldEpisode,
    AniworldSeason,
    AniworldSeries,
    FilmoEpisode,
    FilmPalastEpisode,
    HanimeTVEpisode,
    HanimeTVSeason,
    HanimeTVSeries,
    MangaFireToChapter,
    MangaFireToSeries,
    MegaKinoEpisode,
    MoflixEpisode,
    MoflixSeason,
    SerienstreamEpisode,
    SerienstreamSeason,
    SerienstreamSeries,
)
from .models.broadcasters import DMAXEpisode, ZDFEpisode
from .models.broadcasters.collection import ZDF_COLLECTION_PATTERN, zdf_title
from .models.broadcasters.dmax import DMAX_COLLECTION_PATTERN, DMAX_PATTERN, dmax_title
from .models.broadcasters.zdf import ZDF_PATTERN
from .models.filmo_to.episode import FILMO_EPISODE_PATTERN
from .models.moflix_stream.client import canonical_title_url


@dataclass(frozen=True)
class Provider:
    name: str
    series_pattern: Pattern[str] | None = None
    season_pattern: Pattern[str] | None = None
    episode_pattern: Pattern[str] | None = None

    series_cls: type | None = None
    season_cls: type | None = None
    episode_cls: type | None = None


PROVIDERS = [
    Provider(
        name="ZDF",
        series_pattern=ZDF_COLLECTION_PATTERN,
        episode_pattern=ZDF_PATTERN,
        series_cls=zdf_title,
        episode_cls=ZDFEpisode,
    ),
    Provider(
        name="DMAX",
        series_pattern=DMAX_COLLECTION_PATTERN,
        episode_pattern=DMAX_PATTERN,
        series_cls=dmax_title,
        episode_cls=DMAXEpisode,
    ),
    Provider(
        name="AniWorld",
        series_pattern=H0MELAB_SERIES_PATTERN,
        season_pattern=H0MELAB_SEASON_PATTERN,
        episode_pattern=H0MELAB_EPISODE_PATTERN,
        series_cls=AniworldSeries,
        season_cls=AniworldSeason,
        episode_cls=AniworldEpisode,
    ),
    Provider(
        name="HanimeTV",
        series_pattern=HANIME_TV_SERIES_PATTERN,
        episode_pattern=HANIME_TV_SERIES_PATTERN,
        series_cls=HanimeTVSeries,
        season_cls=HanimeTVSeason,
        episode_cls=HanimeTVEpisode,
    ),
    Provider(
        name="MegaKino",
        series_pattern=MEGAKINO_SERIES_PATTERN,
        series_cls=MegaKinoEpisode,
        season_cls=None,
        episode_cls=MegaKinoEpisode,
    ),
    Provider(
        name="Filmo",
        episode_pattern=FILMO_EPISODE_PATTERN,
        # A movie page is the whole title, so it stands in as its own series the
        # way MegaKino and FilmPalast do. No series_pattern: the CLI keeps
        # treating the URL as an episode, only the web UI asks for the series.
        series_cls=FilmoEpisode,
        season_cls=None,
        episode_cls=FilmoEpisode,
    ),
    Provider(
        name="FilmPalast",
        series_pattern=FILMPALAST_SERIES_PATTERN,
        series_cls=FilmPalastEpisode,
        season_cls=None,
        episode_cls=FilmPalastEpisode,
    ),
    Provider(
        name="MangaFire",
        series_pattern=MANGA_FIRE_SERIES_PATTERN,
        season_pattern=MANGA_FIRE_CHAPTER_PATTERN,
        episode_pattern=MANGA_FIRE_CHAPTER_PATTERN,
        series_cls=MangaFireToSeries,
        season_cls=MangaFireToChapter,
        episode_cls=MangaFireToChapter,
    ),
    Provider(
        name="SerienStream",
        series_pattern=SERIENSTREAM_SERIES_PATTERN,
        season_pattern=SERIENSTREAM_SEASON_PATTERN,
        episode_pattern=SERIENSTREAM_EPISODE_PATTERN,
        series_cls=SerienstreamSeries,
        season_cls=SerienstreamSeason,
        episode_cls=SerienstreamEpisode,
    ),
    Provider(
        name="Moflix",
        series_pattern=MOFLIX_SERIES_PATTERN,
        episode_pattern=MOFLIX_SERIES_PATTERN,
        series_cls=MoflixEpisode,
        season_cls=MoflixSeason,
        episode_cls=MoflixEpisode,
    ),
]


def normalize_url(url: str) -> str:
    if not url:
        return url

    url = url.strip()

    if MOFLIX_SERIES_PATTERN.fullmatch(url):
        return canonical_title_url(url)

    parsed = urlparse(url)
    path = parsed.path

    # --- SerienStream alias handling ---
    # Some endpoints use /serie/stream/<slug>; normalize to /serie/<slug>.
    if path.startswith("/serie/stream/"):
        slug = path[len("/serie/stream/") :].strip("/")
        if slug:
            path = f"/serie/{slug}"

    # remove trailing slash
    path = path.rstrip("/")

    return urlunparse(parsed._replace(path=path))


def resolve_provider(url: str) -> Provider:
    url = normalize_url(url)

    for provider in PROVIDERS:
        if provider.series_pattern and provider.series_pattern.fullmatch(url):
            return provider
        if provider.season_pattern and provider.season_pattern.fullmatch(url):
            return provider
        if provider.episode_pattern and provider.episode_pattern.fullmatch(url):
            return provider

    raise ValueError(f"Unsupported URL: {url}")
