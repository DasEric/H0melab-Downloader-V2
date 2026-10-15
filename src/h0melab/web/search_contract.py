"""Source capabilities and deterministic, title-based search relevance.

Interactive search is deliberately stricter than a provider's full-text search.
Every meaningful query word must be explained by a title/alias (or author for
catalogs). Watchlist identity matching remains separate and stricter still.
"""

import html
import re
import unicodedata

CATEGORIES = {
    "movie": ("Filme", "Movies"),
    "series": ("Serien / Anime", "Series / Anime"),
    "manga": ("Manga", "Manga"),
    "ebook": ("E-Books", "eBooks"),
    "audiobook": ("Hörbücher", "Audio books"),
    "podcast": ("Podcasts", "Podcasts"),
}

SITE_TYPES = {
    "aniworld": ("movie", "series"),
    "sto": ("series",),
    "megakino": ("movie", "series"),
    "mangafire": ("manga",),
    "htv": ("series",),
    "kinox": ("movie", "series"),
    "burningseries": ("series",),
    "filmpalast": ("movie",),
    "filmo": ("movie",),
    "cineby": ("movie", "series"),
    "moflix": ("movie", "series"),
}

BROWSE_PATHS = {
    "aniworld": ("api/new-animes", "api/popular-animes"),
    "sto": ("api/new-series", "api/popular-series"),
    "megakino": ("api/popular-movies",),
    "mangafire": ("api/mangafire-trending",),
    "htv": ("api/htv-trending",),
    "kinox": ("api/kinox-movies",),
    "burningseries": ("api/burningseries-series",),
    "filmpalast": ("api/filmpalast-movies",),
    "filmo": ("api/filmo-movies",),
    "cineby": ("api/cineby-movies",),
    "moflix": ("api/moflix-movies",),
}

_STOP_WORDS = {
    "the",
    "a",
    "an",
    "der",
    "die",
    "das",
    "ein",
    "eine",
    "film",
    "movie",
    "serie",
    "series",
}


def normalized(value):
    value = html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", value, flags=re.UNICODE))


def relevance(item, query):
    query_text = normalized(query)
    words = query_text.split()
    if not words:
        return 0
    year = (
        next((w for w in words if re.fullmatch(r"(?:19|20)\d{2}", w)), None)
        if len(words) > 1
        else None
    )
    if year:
        known = str(item.get("year") or item.get("release_year") or "")[:4]
        embedded = re.search(r"\b(?:19|20)\d{2}\b", str(item.get("title") or ""))
        known = known or (embedded.group() if embedded else "")
        if known and known != year:
            return 0
        words.remove(year)
    significant = [w for w in words if w not in _STOP_WORDS] or words
    if not significant:
        return 0
    phrase = " ".join(words)
    aliases = item.get("alternative_titles") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    titles = [item.get("title"), item.get("name"), item.get("original_title"), *aliases]
    author = (
        normalized(item.get("author"))
        if item.get("media_kind") in ("ebook", "audiobook", "podcast")
        else ""
    )
    best = 0
    for title in titles:
        text = normalized(title)
        if not text:
            continue
        tokens = (text + " " + author).split()
        if not all(
            any(w == t or (len(w) >= 3 and t.startswith(w)) for t in tokens)
            for w in significant
        ):
            continue
        score = (
            1000
            if text == query_text or text == phrase
            else 900
            if text.startswith(phrase)
            else 800
        )
        best = max(best, score - min(100, max(0, len(tokens) - len(significant))))
    return best


def relevant_results(items, query):
    found = [{**item, "relevance": relevance(item, query)} for item in items]
    return sorted(
        (item for item in found if item["relevance"] > 0),
        key=lambda item: (
            -item["relevance"],
            normalized(item.get("title")),
            str(item.get("url") or item.get("id")),
        ),
    )


def media_type(site, item):
    """Only return a type backed by metadata or an unambiguous source/URL."""
    value = item.get("media_type") or item.get("media_kind") or item.get("type")
    if not isinstance(value, str):
        value = ""
    value = {"tv": "series", "movies": "movie"}.get(value, value)
    if value in SITE_TYPES.get(site, ()):
        return value
    if isinstance(item.get("is_series"), bool):
        return "series" if item["is_series"] else "movie"
    if isinstance(item.get("is_movie"), bool):
        return "movie" if item["is_movie"] else "series"
    url = str(item.get("url") or "").lower()
    if site == "megakino":
        return "series" if "/serials/" in url else "movie"
    if site == "cineby":
        if "/tv/" in url:
            return "series"
        if "/movie/" in url:
            return "movie"
    types = SITE_TYPES.get(site, ())
    return types[0] if len(types) == 1 else ""


def source_capabilities():
    from ..catalog import source_summaries
    from .media import SITE_LABELS, SITE_ORDER
    from .settings_store import enabled_sites

    enabled = enabled_sites()
    sources = [
        {
            "id": site,
            "label": SITE_LABELS[site],
            "media_types": list(SITE_TYPES[site]),
            "backend": "video",
            "enabled": bool(enabled.get(site)),
            "browse_paths": list(BROWSE_PATHS.get(site, ())),
        }
        for site in SITE_ORDER
    ]
    sources.extend(
        {
            "id": source["key"],
            "label": source["label"],
            "media_types": [source["media_kind"]],
            "backend": "catalog",
            "enabled": True,
            "browse_paths": [],
        }
        for source in source_summaries()
    )
    return {"schema_version": 1, "sources": sources}
