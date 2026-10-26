"""Season-page identity, independent of MegaKino's genre/category URLs."""

import re

_SEASON_TITLE = re.compile(
    r"^(?P<title>.+?)(?:\s*[-–—:]\s*|\s+)"
    r"(?:(?:Staffel|Season)\s+(?P<after>\d{1,3})|"
    r"(?P<before>\d{1,3})\.?\s+(?:Staffel|Season))\s*$",
    re.IGNORECASE,
)
_FULL_EPISODE = re.compile(
    r"\b(?:S(?P<season>\d{1,3})\s*E(?P<episode>\d{1,4})|"
    r"(?P<xseason>\d{1,3})x(?P<xepisode>\d{1,4}))\b",
    re.IGNORECASE,
)
_EPISODE_LABEL = re.compile(
    r"(?:(?:Episode|Folge|Ep\.?)\s*(?P<after>\d{1,4})|"
    r"(?P<before>\d{1,4})\.?\s*(?:Episode|Folge))"
    r"(?:\s*[-:]\s*.+)?",
    re.IGNORECASE,
)


def parse_season_title(title):
    """Return (series title, explicit season), never an assumed season one."""
    match = _SEASON_TITLE.fullmatch(title or "")
    if match and (name := match["title"].strip()):
        return name, int(match["after"] or match["before"])
    return None


def episode_numbers(title, label):
    page = parse_season_title(title)
    full = _FULL_EPISODE.search(label)
    if full:
        season = int(full["season"] or full["xseason"])
        number = int(full["episode"] or full["xepisode"])
        if page and season != page[1]:
            raise ValueError("The page and episode label disagree on the season number")
    else:
        episode = _EPISODE_LABEL.fullmatch(label)
        if not page or not episode:
            return None
        season, number = page[1], int(episode["after"] or episode["before"])
    if number < 1:
        raise ValueError("The source returned an invalid episode number")
    return season, number
