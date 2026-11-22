"""Native broadcaster clients; no yt-dlp runtime dependency."""

from .episode import DMAXEpisode, ZDFEpisode

__all__ = ["DMAXEpisode", "ZDFEpisode"]
