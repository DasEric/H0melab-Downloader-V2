"""Catalog source registry."""

from .common import CatalogError
from .sources import (
    AnnasArchiveSource,
    AudioAnarchySource,
    GutenbergSource,
    LibriVoxSource,
    ListenNotesSource,
    PlayerFMSource,
    StandardEbooksSource,
)

_SOURCES = {
    source.key: source
    for source in (
        AnnasArchiveSource(), GutenbergSource(), StandardEbooksSource(),
        AudioAnarchySource(), LibriVoxSource(), ListenNotesSource(), PlayerFMSource(),
    )
}


def get_source(key):
    try:
        return _SOURCES[str(key)]
    except KeyError as exc:
        raise CatalogError("Unknown catalog source") from exc


def source_summaries():
    return [source.summary() for source in _SOURCES.values()]
