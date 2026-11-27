from types import SimpleNamespace

import pytest

from h0melab.web import seerr_fallback as fallback
from h0melab.web import worker


@pytest.fixture
def setup(monkeypatch):
    identity = {
        "media_type": "movie",
        "tmdb_id": 7,
        "title": "Movie",
        "release_year": 2024,
    }
    entry = {
        "url": "https://one/movie",
        "season": 0,
        "episode": 0,
        "_seerr_identity": identity,
    }
    request = {
        "selected_url": entry["url"],
        "candidates": [
            {**identity, "url": "https://one/movie"},
            {**identity, "url": "https://two/movie"},
            {**identity, "url": "https://two/movie"},
            {**identity, "url": "https://wrong/movie", "tmdb_id": 8},
        ],
    }
    monkeypatch.setattr(fallback.seerr, "get_request", lambda **kw: request)
    monkeypatch.setattr(fallback.seerr_processing, "stopped", lambda item: False)
    monkeypatch.setattr(fallback.seerr_files, "existing", lambda entry: False)
    monkeypatch.setattr(fallback.db, "update_queue_progress", lambda *args: None)
    monkeypatch.setattr(
        worker, "_build_episode", lambda *args: (None, SimpleNamespace())
    )
    return {"id": 1}, entry, request


def test_movie_falls_back_once_and_preserves_identity(setup, monkeypatch):
    item, entry, request = setup
    calls = []

    def download(item, candidate, episode, **kw):
        calls.append(candidate)
        if len(calls) == 1:
            raise OSError("host unavailable")
        assert kw["source_request"]["selected_url"] == "https://two/movie"
        assert candidate["_seerr_identity"] == entry["_seerr_identity"]
        return False

    monkeypatch.setattr(fallback.seerr_files, "download", download)
    assert fallback.download_entry(item, entry, None, 0) is False
    assert [c["url"] for c in calls] == ["https://one/movie", "https://two/movie"]
    assert request["selected_url"] == entry["url"]


def test_exhaustion_is_bounded(setup, monkeypatch):
    item, entry, _ = setup
    calls = []

    def build(url, *args):
        calls.append(url)
        raise OSError("failed")

    monkeypatch.setattr(worker, "_build_episode", build)
    with pytest.raises(OSError):
        fallback.download_entry(item, entry, None, 0)
    assert len(calls) == 2


def test_pause_never_switches_source(setup, monkeypatch):
    from h0melab.models.common.common import DownloadPaused

    item, entry, _ = setup

    def build(*args):
        raise DownloadPaused()

    monkeypatch.setattr(worker, "_build_episode", build)
    monkeypatch.setattr(
        fallback, "alternatives", lambda *args: pytest.fail("No fallback on pause")
    )
    with pytest.raises(DownloadPaused):
        fallback.download_entry(item, entry, None, 0)


@pytest.mark.parametrize("existing,stopped", [(True, False), (False, True)])
def test_completed_or_cancelled_does_not_download(
    setup, monkeypatch, existing, stopped
):
    item, entry, _ = setup
    monkeypatch.setattr(fallback.seerr_files, "existing", lambda e: existing)
    monkeypatch.setattr(fallback.seerr_processing, "stopped", lambda i: stopped)
    monkeypatch.setattr(
        worker, "_build_episode", lambda *args: pytest.fail("No download")
    )
    assert fallback.download_entry(item, entry, None, 0) is (None if stopped else True)


@pytest.mark.parametrize(
    "wrong,duplicate", [(False, False), (True, False), (False, True)]
)
def test_episode_fallback_matches_only_requested_episode(
    setup, monkeypatch, wrong, duplicate
):
    item, entry, request = setup
    identity = entry["_seerr_identity"]
    identity["media_type"] = "tv"
    entry.update(season=2, episode=3)
    for hit in request["candidates"]:
        hit["media_type"] = "tv"
    request["episode_review"] = {
        "canonical": [{"season_number": 2, "episode_number": 3, "name": "The Return"}]
    }
    row = {
        "url": "https://two/s2/e3",
        "season_number": 2,
        "episode_number": 4 if wrong else 3,
        "episode_title": "The Return",
    }
    monkeypatch.setattr(
        fallback.seerr_processing,
        "episode_rows",
        lambda *args: [row, row] if duplicate else [row],
    )
    result = list(fallback.alternatives(item, entry))
    if wrong:
        assert result == []
    else:
        candidate, evidence = result[0]
        assert candidate["season"] == 2 and candidate["episode"] == 3
        assert candidate["url"] == row["url"]
        assert evidence["episode_review"]["source"] == [row]
        assert evidence["episode_mapping"] == []


def test_each_episode_retries_independently(setup, monkeypatch):
    item, original, _ = setup
    calls = []
    monkeypatch.setattr(
        fallback,
        "alternatives",
        lambda item, entry: iter(
            [({**entry, "url": "https://two/episode"}, {"selected_url": "https://two"})]
        ),
    )

    def download(item, entry, episode, **kw):
        calls.append((entry["episode"], entry["url"]))
        if entry["episode"] == 2 and entry["url"] == original["url"]:
            raise OSError("Only episode two fails")
        return False

    monkeypatch.setattr(fallback.seerr_files, "download", download)
    for number in (1, 2, 3):
        assert (
            fallback.download_entry(
                item, {**original, "episode": number}, None, number - 1
            )
            is False
        )
    assert calls == [
        (1, original["url"]),
        (2, original["url"]),
        (2, "https://two/episode"),
        (3, original["url"]),
    ]


def test_cancel_during_failure_prevents_next_source(setup, monkeypatch):
    item, entry, _ = setup
    cancelled = False
    calls = []

    def build(*args):
        nonlocal cancelled
        calls.append(args)
        cancelled = True
        raise OSError("cancelled download")

    monkeypatch.setattr(worker, "_build_episode", build)
    monkeypatch.setattr(fallback.seerr_processing, "stopped", lambda item: cancelled)
    assert fallback.download_entry(item, entry, None, 0) is None
    assert len(calls) == 1
