"""Read-only source metadata for API clients; no native UI/search filtering.

Concrete media types describe sources/results. Search grouping and relevance
policy belong exclusively to the Jellyfin connector.
"""

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
