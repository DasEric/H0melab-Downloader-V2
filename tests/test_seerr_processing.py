import json
from types import SimpleNamespace

import pytest

from h0melab.web import (
    db,
    media_files,
    media_targets,
    seerr,
    seerr_files,
    seerr_processing,
    worker,
)


@pytest.fixture
def setup_media(tmp_path, monkeypatch):
    monkeypatch.setenv(seerr.ENABLED, "1")
    movies, shows = tmp_path / "movies", tmp_path / "shows"
    movies.mkdir()
    shows.mkdir()
    movie = db.add_custom_path("Movies", str(movies))
    show = db.add_custom_path("Shows", str(shows))
    media_targets.save(movie, show, 0)
    monkeypatch.setattr(seerr, "notify", lambda *a: None)
    return movies, shows


def identity(kind="movie"):
    return {
        "media_type": kind,
        "tmdb_id": 42,
        "imdb_id": "tt1234567",
        "title": "Example",
        "original_title": "Example",
        "release_year": 2020,
        "seasons": [{"season_number": 1}, {"season_number": 2}],
    }


def request(monkeypatch, kind="movie", seasons=None):
    value = identity(kind)
    monkeypatch.setattr(seerr_processing.tmdb, "identity_details", lambda *a: value)
    hit = {
        "url": "https://example.test/show",
        "media_type": kind,
        "tmdb_id": 42,
        "source": "sto",
        "assessment": {"conflict": False},
    }
    monkeypatch.setattr(seerr_processing, "candidates", lambda _: [hit])
    monkeypatch.setattr(
        seerr_processing.seerr_matching, "enrich_candidate", lambda _: dict(hit)
    )
    qid, _ = seerr.accept(
        {
            "request_id": 1,
            "title": "Example",
            "media_type": kind,
            "tmdb_id": 42,
            "imdb_id": "tt1234567",
            "seasons": seasons or [],
        }
    )
    return qid


class Episode:
    _file_extension = "mp4"

    def __init__(self, kind="movie", season=0, number=0):
        self.video_identity = {
            "media_type": "movie" if kind == "movie" else "series",
            "season": season,
            "episode": number,
        }

    @property
    def _episode_path(self):
        return media_files.routed_episode_path(self)

    def download(self):
        self._episode_path.write_bytes(b"finished-media")


def test_movie_pipeline_uses_existing_queue_and_flat_movie_target(
    setup_media, monkeypatch
):
    qid = request(monkeypatch)
    monkeypatch.setattr(
        worker, "_build_episode", lambda *a: (SimpleNamespace(name="Fake"), Episode())
    )
    worker._process(db.get_queue_item(qid))
    assert (setup_media[0] / "Example (2020).mp4").read_bytes() == b"finished-media"
    assert db.get_queue_item(qid)["status"] == "completed"
    assert seerr.get_request(queue_id=qid)["status"] == "completed"
    assert len(db.get_queue_item(qid)["episodes"]) > 2
    worker._process(db.get_queue_item(qid))
    assert seerr.get_request(queue_id=qid)["status"] == "already_present"


def test_series_only_requested_season_and_shared_folder(setup_media, monkeypatch):
    qid = request(monkeypatch, "tv", [2])
    monkeypatch.setattr(
        seerr_processing,
        "episode_rows",
        lambda *a: [
            {
                "url": "https://example.test/ep",
                "season_number": 2,
                "episode_number": 1,
                "episode_title": "Pilot",
            }
        ],
    )
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 2,
                    "episode_number": 1,
                    "name": "Pilot",
                }
            ]
        },
    )
    monkeypatch.setattr(
        worker,
        "_build_episode",
        lambda *a: (SimpleNamespace(name="Fake"), Episode("tv", 2, 1)),
    )
    worker._process(db.get_queue_item(qid))
    assert (setup_media[1] / "Example" / "Season 02" / "Example S02E01.mp4").exists()
    assert not (setup_media[1] / "Example" / "Season 01").exists()
    changed = {**identity("tv"), "title": "Renamed"}
    assert seerr_files.folder(changed) == "Example"


@pytest.mark.parametrize("kind", ["movie", "tv"])
def test_unknown_existing_file_is_preserved(setup_media, monkeypatch, kind):
    qid = request(monkeypatch, kind, [1] if kind == "tv" else None)
    if kind == "tv":
        monkeypatch.setattr(
            seerr_processing,
            "episode_rows",
            lambda *a: [
                {
                    "url": "https://example.test/ep",
                    "season_number": 1,
                    "episode_number": 1,
                    "episode_title": "Pilot",
                }
            ],
        )
        monkeypatch.setattr(
            seerr_processing.tmdb,
            "season_details",
            lambda *a: {
                "episodes": [
                    {
                        "series_tmdb_id": 42,
                        "season_number": 1,
                        "episode_number": 1,
                        "name": "Pilot",
                    }
                ]
            },
        )
    entry = {
        "_seerr_identity": identity(kind),
        "season": int(kind == "tv"),
        "episode": int(kind == "tv"),
    }
    _, path = seerr_files.output(entry, "mp4")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"foreign")
    before = path.stat().st_mtime_ns
    episode = Episode(kind, entry["season"], entry["episode"])
    monkeypatch.setattr(
        episode, "download", lambda: pytest.fail("No download over unknown media")
    )
    monkeypatch.setattr(
        worker, "_build_episode", lambda *a: (SimpleNamespace(name="Fake"), episode)
    )
    worker._process(db.get_queue_item(qid))
    assert path.read_bytes() == b"foreign"
    assert path.stat().st_mtime_ns == before
    item = db.get_queue_item(qid)
    assert item["status"] == "queued" and item["pause_requested"]
    assert item["current_episode"] == 0
    assert seerr.get_request(queue_id=qid)["status"] == "manual_review"
    assert "no matching identity receipt" in seerr.get_request(queue_id=qid)["error"]
    assert seerr_files.existing(entry) is None


def test_ambiguous_episode_pauses_instead_of_empty_success(setup_media, monkeypatch):
    qid = request(monkeypatch, "tv", [1])
    monkeypatch.setattr(seerr_processing, "episode_rows", lambda *a: [])
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": 1,
                    "name": "Pilot",
                }
            ]
        },
    )
    worker._process(db.get_queue_item(qid))
    item = db.get_queue_item(qid)
    assert item["pause_requested"] and item["status"] == "queued"
    assert seerr.get_request(queue_id=qid)["status"] == "manual_review"


@pytest.mark.parametrize("kind", ["movie", "tv"])
def test_targets_are_dynamic_and_site_defaults_are_irrelevant(
    setup_media, monkeypatch, tmp_path, kind
):
    qid = request(monkeypatch, kind, [1] if kind == "tv" else [])
    wrong_root = tmp_path / "site-default"
    wrong_root.mkdir()
    db.add_custom_path("Conflicting site default", str(wrong_root), default_sites="sto")
    next_root = tmp_path / "next"
    next_root.mkdir()
    pid = db.add_custom_path("Next", str(next_root))
    targets = media_targets.read()
    media_targets.save(
        pid if kind == "movie" else targets["movie"],
        pid if kind == "tv" else targets["series"],
        targets["revision"],
    )
    if kind == "tv":
        monkeypatch.setattr(
            seerr_processing,
            "episode_rows",
            lambda *a: [
                {
                    "url": "https://example.test/ep1",
                    "season_number": 1,
                    "episode_number": 1,
                    "episode_title": "First",
                }
            ],
        )
        monkeypatch.setattr(
            seerr_processing.tmdb,
            "season_details",
            lambda *a: {
                "episodes": [
                    {
                        "series_tmdb_id": 42,
                        "season_number": 1,
                        "episode_number": 1,
                        "name": "First",
                    }
                ]
            },
        )
    monkeypatch.setattr(
        worker,
        "_build_episode",
        lambda *a: (SimpleNamespace(name="Fake"), Episode(kind, 1, 1)),
    )
    worker._process(db.get_queue_item(qid))
    relative = (
        "Example (2020).mp4"
        if kind == "movie"
        else "Example/Season 01/Example S01E01.mp4"
    )
    assert (next_root / relative).is_file()
    assert not list(setup_media[0].iterdir())
    assert not list(setup_media[1].iterdir())
    assert not list(wrong_root.iterdir())
    assert db.get_queue_item(qid)["custom_path_id"] == pid
    assert seerr.get_request(request_id=1)["target_path"] == str(next_root)


def test_metadata_outage_does_not_start_download(setup_media, monkeypatch):
    qid = request(monkeypatch)

    def fail(*a):
        raise seerr_processing.tmdb.TMDBError("offline")

    monkeypatch.setattr(seerr_processing.tmdb, "identity_details", fail)
    worker._process(db.get_queue_item(qid))
    assert db.get_queue_item(qid)["pause_requested"]
    assert seerr.get_request(queue_id=qid)["identity_status"] == "metadata_failed"


def test_explicit_alternate_episode_numbering_uses_canonical_filename(
    setup_media, monkeypatch
):
    qid = request(monkeypatch, "tv", [1])
    url = "https://example.test/show/episode-13"
    monkeypatch.setattr(
        seerr_processing,
        "episode_rows",
        lambda *a: [
            {
                "url": url,
                "season_number": 1,
                "episode_number": 13,
                "title_en": "Different localized title",
            }
        ],
    )
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": 1,
                    "name": "Pilot",
                }
            ]
        },
    )
    assert not seerr.prepare(db.get_queue_item(qid))
    seerr.review_request(
        1,
        {
            "selected_url": "https://example.test/show",
            "reason": "Checked alternate provider numbering against TMDB",
            "episode_mapping": [{"url": url, "season_number": 1, "episode_number": 1}],
        },
    )
    monkeypatch.setattr(
        worker,
        "_build_episode",
        lambda *a: (SimpleNamespace(name="Fake"), Episode("tv", 1, 13)),
    )
    worker._process(db.get_queue_item(qid))
    assert db.get_queue_item(qid)["status"] == "completed"
    assert (setup_media[1] / "Example" / "Season 01" / "Example S01E01.mp4").is_file()
    assert not list(setup_media[1].rglob("*E13*"))


def test_manual_mapping_does_not_override_foreign_series(setup_media, monkeypatch):
    qid = request(monkeypatch, "tv", [1])
    url = "https://example.test/show/episode-13"
    monkeypatch.setattr(
        seerr_processing,
        "episode_rows",
        lambda *a: [
            {
                "url": url,
                "season_number": 1,
                "episode_number": 13,
                "series_tmdb_id": 999,
            }
        ],
    )
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": 1,
                    "name": "Pilot",
                }
            ]
        },
    )
    assert not seerr.prepare(db.get_queue_item(qid))
    seerr.review_request(
        1,
        {
            "selected_url": "https://example.test/show",
            "reason": "Explicit review",
            "episode_mapping": [{"url": url, "season_number": 1, "episode_number": 1}],
        },
    )
    assert not seerr.prepare(db.get_queue_item(qid))
    assert not list(setup_media[1].rglob("*.mp4"))


def test_disabled_integration_pauses_accepted_request(setup_media, monkeypatch):
    qid = request(monkeypatch)
    monkeypatch.setenv(seerr.ENABLED, "0")

    def unexpected(*args):
        raise AssertionError("Disabled integration must not query metadata")

    monkeypatch.setattr(seerr_processing.tmdb, "identity_details", unexpected)
    worker._process(db.get_queue_item(qid))
    assert db.get_queue_item(qid)["pause_requested"] == 1
    assert seerr.get_request(request_id=1)["status"] == "manual_review"
    assert not list(setup_media[0].rglob("*.mp4"))


def test_automatic_selection_does_not_become_manual_approval_on_retry(
    setup_media, monkeypatch
):
    qid = request(monkeypatch)
    assert seerr.prepare(db.get_queue_item(qid))
    seerr.update(1, status="manual_review")
    weaker = {
        "url": "https://example.test/show",
        "media_type": "movie",
        "title": "Example",
        "source": "sto",
        "assessment": {"conflict": False},
    }
    monkeypatch.setattr(seerr_processing, "candidates", lambda _: [weaker])
    assert not seerr.prepare(db.get_queue_item(qid))
    assert "sufficient identity evidence" in seerr.get_request(request_id=1)["error"]
    assert not list(setup_media[0].rglob("*.mp4"))


def test_manual_selection_still_requires_confirmed_media_type(setup_media, monkeypatch):
    qid = request(monkeypatch)
    seerr.update(
        1, selected_url="https://example.test/show", review_reason="Admin confirmation"
    )
    untyped = {
        "url": "https://example.test/show",
        "tmdb_id": 42,
        "assessment": {"conflict": False},
    }
    monkeypatch.setattr(seerr_processing, "candidates", lambda _: [untyped])
    assert not seerr.prepare(db.get_queue_item(qid))
    assert not list(setup_media[0].rglob("*.mp4"))


def test_unclear_episode_snapshot_is_available_for_manual_review(
    setup_media, monkeypatch
):
    qid = request(monkeypatch, "tv", [1])
    source = {
        "url": "https://example.test/episode",
        "season_number": 1,
        "episode_number": 9,
        "title_en": "Localized title",
    }
    monkeypatch.setattr(seerr_processing, "episode_rows", lambda *a: [source])
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": 1,
                    "name": "Pilot",
                }
            ]
        },
    )
    assert not seerr.prepare(db.get_queue_item(qid))
    detail = seerr.get_request(request_id=1)["episode_review"]
    assert detail["source"] == [source]
    assert detail["canonical"] == [
        {"season_number": 1, "episode_number": 1, "name": "Pilot"}
    ]
    assert detail["provider_url"] == "https://example.test/show"


def test_explicit_source_season_does_not_expand_requested_downloads(
    setup_media, monkeypatch
):
    qid = request(monkeypatch, "tv", [1])
    url = "https://example.test/show/season-3/episode-13"
    inspected = []

    def rows(hit, seasons):
        inspected.append(seasons)
        return (
            [
                {
                    "url": url,
                    "season_number": 3,
                    "episode_number": 13,
                    "title_en": "Source title",
                }
            ]
            if 3 in seasons
            else []
        )

    monkeypatch.setattr(seerr_processing, "episode_rows", rows)
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": 1,
                    "name": "Pilot",
                }
            ]
        },
    )
    assert not seerr.prepare(db.get_queue_item(qid))
    seerr.review_request(
        1,
        {
            "selected_url": "https://example.test/show",
            "reason": "Verified different season organization",
            "episode_mapping": [
                {
                    "url": url,
                    "season_number": 1,
                    "episode_number": 1,
                    "source_season_number": 3,
                }
            ],
        },
    )
    monkeypatch.setattr(
        worker,
        "_build_episode",
        lambda *a: (SimpleNamespace(name="Fake"), Episode("tv", 3, 13)),
    )
    worker._process(db.get_queue_item(qid))
    assert inspected == [[1], [1, 3], [3]]
    assert db.get_queue_item(qid)["total_episodes"] == 1
    assert (setup_media[1] / "Example" / "Season 01" / "Example S01E01.mp4").is_file()
    assert not (setup_media[1] / "Example" / "Season 03").exists()


@pytest.mark.parametrize("force", [False, True])
def test_cancel_during_tmdb_lookup_never_starts_download(
    setup_media, monkeypatch, force
):
    qid = request(monkeypatch)
    db.set_queue_status(qid, "running")

    def lookup(*args):
        db.cancel_queue_item(qid, force=force)
        return identity()

    monkeypatch.setattr(seerr_processing.tmdb, "identity_details", lookup)
    monkeypatch.setattr(
        worker,
        "_build_episode",
        lambda *a: pytest.fail("cancelled request built an episode"),
    )
    worker._process(db.get_queue_item(qid))
    assert db.get_queue_item(qid)["status"] == "cancelled"
    assert seerr.get_request(request_id=1)["status"] == "cancelled"


def test_cancel_during_failed_metadata_lookup_is_not_resurrected(
    setup_media, monkeypatch
):
    qid = request(monkeypatch)
    db.set_queue_status(qid, "running")

    def lookup(*args):
        db.cancel_queue_item(qid, force=True)
        raise seerr_processing.tmdb.TMDBError("outage")

    monkeypatch.setattr(seerr_processing.tmdb, "identity_details", lookup)
    worker._process(db.get_queue_item(qid))
    assert db.get_queue_item(qid)["status"] == "cancelled"
    assert not db.get_queue_item(qid)["pause_requested"]
    assert seerr.get_request(request_id=1)["status"] == "cancelled"


def test_pause_during_source_search_preserves_control(setup_media, monkeypatch):
    qid = request(monkeypatch)
    db.set_queue_status(qid, "running")

    def search(*args):
        db.pause_queue_item(qid)
        return []

    monkeypatch.setattr(seerr_processing, "candidates", search)
    worker._process(db.get_queue_item(qid))
    assert db.get_queue_item(qid)["pause_requested"] == 1
    assert seerr.get_request(request_id=1)["status"] == "paused"


def test_provider_exception_secrets_are_not_persisted_or_logged(
    setup_media, monkeypatch, caplog
):
    qid = request(monkeypatch)
    secret = "UNIQUE_SECRET_IN_SIGNED_STREAM_URL"

    class Broken(Episode):
        def download(self):
            raise ValueError("https://stream.example/video?token=" + secret)

    monkeypatch.setattr(
        worker, "_build_episode", lambda *a: (SimpleNamespace(name="Fake"), Broken())
    )
    worker._process(db.get_queue_item(qid))
    item = db.get_queue_item(qid)
    assert item["status"] == "failed"
    assert secret not in item["errors"]
    assert secret not in caplog.text
    record = seerr.get_request(request_id=1)
    assert record["status"] == "failed" and record["error"]
    assert secret not in str(record)


def test_transient_metadata_retry_is_bounded_and_uses_existing_queue(
    setup_media, monkeypatch, queue_item
):
    qid = request(monkeypatch)

    def unavailable(*args):
        raise seerr_processing.tmdb.TMDBTransientError("temporary")

    monkeypatch.setattr(seerr_processing.tmdb, "identity_details", unavailable)
    for count in (1, 2, 3):
        assert not seerr.prepare(db.get_queue_item(qid))
        record = seerr.get_request(request_id=1)
        assert record["retry_count"] == count
        assert record["status"] == "waiting_retry"
        assert db.get_queue_item(qid)["not_before"] > 0
        assert worker._claim_next() is None
    other = queue_item()
    assert worker._claim_next()["id"] == other
    db.set_queue_status(other, "completed")
    assert not seerr.prepare(db.get_queue_item(qid))
    assert seerr.get_request(request_id=1)["status"] == "manual_review"
    assert db.get_queue_item(qid)["pause_requested"] == 1
    seerr.retry_request(1)
    assert seerr.get_request(request_id=1)["retry_count"] == 0
    assert db.get_queue_item(qid)["not_before"] == 0


def test_missing_source_for_completed_episode_does_not_block_missing_download(
    setup_media, monkeypatch
):
    qid = request(monkeypatch, "tv", [1])
    old = {"_seerr_identity": identity("tv"), "season": 1, "episode": 1}
    _, old_path = seerr_files.output(old, "mp4")
    old_path.parent.mkdir(parents=True)
    old_path.write_bytes(b"verified first episode")
    seerr_files.record(old, old_path)
    monkeypatch.setattr(
        seerr_processing,
        "episode_rows",
        lambda *a: [
            {
                "url": "https://example.test/ep2",
                "season_number": 1,
                "episode_number": 2,
                "episode_title": "Second",
            }
        ],
    )
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": 1,
                    "name": "First",
                },
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": 2,
                    "name": "Second",
                },
            ]
        },
    )
    built = []

    def build(url, *args):
        built.append(url)
        return SimpleNamespace(name="Fake"), Episode("tv", 1, 2)

    monkeypatch.setattr(worker, "_build_episode", build)
    worker._process(db.get_queue_item(qid))
    assert built == ["https://example.test/ep2"]
    assert old_path.read_bytes() == b"verified first episode"
    assert (old_path.parent / "Example S01E02.mp4").is_file()
    assert db.get_queue_item(qid)["status"] == "completed"


def test_receipt_removed_after_prepare_requires_new_source_resolution(
    setup_media, monkeypatch
):
    qid = request(monkeypatch, "tv", [1])
    old = {"_seerr_identity": identity("tv"), "season": 1, "episode": 1}
    _, path = seerr_files.output(old, "mp4")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"verified")
    seerr_files.record(old, path)
    monkeypatch.setattr(seerr_processing, "episode_rows", lambda *a: [])
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": 1,
                    "name": "First",
                }
            ]
        },
    )
    original = seerr.prepare

    def prepare(item):
        result = original(item)
        path.unlink()
        return result

    monkeypatch.setattr(seerr, "prepare", prepare)
    monkeypatch.setattr(
        worker, "_build_episode", lambda *a: pytest.fail("No verified source URL")
    )
    worker._process(db.get_queue_item(qid))
    assert seerr.get_request(request_id=1)["status"] == "manual_review"
    assert db.get_queue_item(qid)["pause_requested"] == 1


def test_nfo_existing_movie_completes_without_provider_search(setup_media, monkeypatch):
    qid = request(monkeypatch)
    video = setup_media[0] / "Existing film.mkv"
    video.write_bytes(b"original media")
    video.with_suffix(".nfo").write_text(
        "<movie><tmdbid>42</tmdbid><imdbid>tt1234567</imdbid></movie>"
    )
    monkeypatch.setattr(
        seerr_processing,
        "candidates",
        lambda *a: pytest.fail("Existing movie must not require a source"),
    )
    monkeypatch.setattr(
        worker,
        "_build_episode",
        lambda *a: pytest.fail("Existing movie must not construct a provider"),
    )
    worker._process(db.get_queue_item(qid))
    assert db.get_queue_item(qid)["status"] == "completed"
    assert seerr.get_request(request_id=1)["status"] == "already_present"
    assert video.read_bytes() == b"original media"
    assert not (setup_media[0] / "Example (2020).mp4").exists()


def test_provider_valueerror_is_not_exposed(setup_media, monkeypatch, caplog):
    qid = request(monkeypatch)
    secret = "signed-provider-token-do-not-store"

    def fail(*args):
        raise ValueError("https://provider.test/?token=" + secret)

    monkeypatch.setattr(seerr_processing, "candidates", fail)
    worker._process(db.get_queue_item(qid))
    assert seerr.get_request(request_id=1)["status"] == "manual_review"
    assert secret not in str(seerr.get_request(request_id=1))
    assert secret not in str(db.get_queue_item(qid))
    assert secret not in caplog.text


@pytest.mark.parametrize("future", [False, True])
def test_partial_season_downloads_only_verified_available_episodes_then_retry(
    setup_media, monkeypatch, future
):
    qid = request(monkeypatch, "tv", [1])
    canonical = [
        {
            "series_tmdb_id": 42,
            "season_number": 1,
            "episode_number": 1,
            "name": "First",
        },
        {
            "series_tmdb_id": 42,
            "season_number": 1,
            "episode_number": 2,
            "name": "Second",
            "air_date": "2999-01-01" if future else "2020-01-01",
        },
    ]
    rows = [
        {
            "url": "https://example.test/ep1",
            "season_number": 1,
            "episode_number": 1,
            "episode_title": "First",
        },
    ]
    second = {
        "url": "https://example.test/ep2",
        "season_number": 1,
        "episode_number": 2,
        "episode_title": "Second",
    }
    if future:
        rows.append(second)
    monkeypatch.setattr(seerr_processing, "episode_rows", lambda *a: rows)
    monkeypatch.setattr(
        seerr_processing.tmdb, "season_details", lambda *a: {"episodes": canonical}
    )
    built = []

    def build(url, *args):
        built.append(url)
        return SimpleNamespace(name="Fake"), Episode(
            "tv", 1, 1 if url.endswith("ep1") else 2
        )

    monkeypatch.setattr(worker, "_build_episode", build)
    worker._process(db.get_queue_item(qid))
    result = seerr.get_request(request_id=1)
    assert result["status"] == "partial"
    assert db.get_queue_item(qid)["completed_at"] is None
    assert result["episode_review"]["pending"][0]["reason"] == (
        "not_yet_aired" if future else "source_missing"
    )
    assert db.get_queue_item(qid)["status"] == "queued"
    assert db.get_queue_item(qid)["pause_requested"] == 1
    assert built == ["https://example.test/ep1"]
    first = setup_media[1] / "Example" / "Season 01" / "Example S01E01.mp4"
    before = first.stat().st_mtime_ns
    canonical[1]["air_date"] = "2020-01-01"
    if not future:
        rows.append(second)
    seerr.retry_request(1)
    worker._process(db.get_queue_item(qid))
    assert built == ["https://example.test/ep1", "https://example.test/ep2"]
    assert first.stat().st_mtime_ns == before
    assert seerr.get_request(request_id=1)["status"] == "completed"
    assert seerr.get_request(request_id=1)["episode_review"]["pending"] == []
    assert db.get_queue_item(qid)["status"] == "completed"


def test_partial_season_does_not_bypass_conflicting_episode_review(
    setup_media, monkeypatch
):
    qid = request(monkeypatch, "tv", [1])
    monkeypatch.setattr(
        seerr_processing,
        "episode_rows",
        lambda *a: [
            {
                "url": "https://example.test/ep1",
                "season_number": 1,
                "episode_number": 1,
                "episode_title": "First",
            },
            {
                "url": "https://example.test/ep2",
                "season_number": 1,
                "episode_number": 2,
                "episode_title": "Wrong episode",
                "series_tmdb_id": 999,
            },
        ],
    )
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": 1,
                    "name": "First",
                },
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": 2,
                    "name": "Second",
                },
            ]
        },
    )
    monkeypatch.setattr(
        worker,
        "_build_episode",
        lambda *a: pytest.fail("Conflict must pause before downloads"),
    )
    worker._process(db.get_queue_item(qid))
    assert seerr.get_request(request_id=1)["status"] == "manual_review"
    assert not list(setup_media[1].rglob("*.mp4"))


def test_all_existing_series_needs_no_provider(setup_media, monkeypatch):
    qid = request(monkeypatch, "tv", [1])
    entry = {"_seerr_identity": identity("tv"), "season": 1, "episode": 1}
    _, path = seerr_files.output(entry, "mp4")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing series episode")
    seerr_files.record(entry, path)
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": 1,
                    "name": "First",
                }
            ]
        },
    )
    monkeypatch.setattr(
        seerr_processing,
        "candidates",
        lambda *a: pytest.fail("Existing series requires no source"),
    )
    monkeypatch.setattr(
        worker, "_build_episode", lambda *a: pytest.fail("No source construction")
    )
    worker._process(db.get_queue_item(qid))
    assert seerr.get_request(request_id=1)["status"] == "already_present"
    assert db.get_queue_item(qid)["status"] == "completed"
    assert path.read_bytes() == b"existing series episode"


def test_restart_keeps_seerr_cancellation_and_request_state(setup_media, monkeypatch):
    qid = request(monkeypatch)
    db.set_queue_status(qid, "running")
    seerr.update(1, status="downloading")
    db.cancel_queue_item(qid)
    assert db.reset_stale_running()
    assert db.get_queue_item(qid)["status"] == "cancelled"
    assert seerr.get_request(request_id=1)["status"] == "cancelled"
    assert db.claim_next_queued() is None


@pytest.mark.parametrize("control", ["cancel", "pause"])
def test_control_during_fresh_identity_stops_before_download(
    setup_media, monkeypatch, control
):
    qid = request(monkeypatch)

    def fresh(hit):
        if control == "cancel":
            db.cancel_queue_item(qid)
        else:
            with db.session() as conn:
                conn.execute(
                    "UPDATE download_queue SET pause_requested=1 WHERE id=?", (qid,)
                )
        return {"media_type": "movie", "tmdb_id": 42}

    monkeypatch.setattr(seerr_processing.seerr_matching, "enrich_candidate", fresh)
    episode = Episode()
    monkeypatch.setattr(
        episode, "download", lambda: pytest.fail("No bytes after control")
    )
    monkeypatch.setattr(
        worker, "_build_episode", lambda *a: (SimpleNamespace(name="Fake"), episode)
    )
    worker._process(db.get_queue_item(qid))
    assert seerr.get_request(request_id=1)["status"] == (
        "cancelled" if control == "cancel" else "paused"
    )
    assert not list(setup_media[0].iterdir())


@pytest.mark.parametrize("kind", ["movie", "tv"])
def test_authenticated_webhook_through_worker_to_files(
    setup_media, monkeypatch, auth_client, kind
):
    # Set up external metadata/provider boundaries without pre-accepting a request.
    value = identity(kind)
    hit = {
        "url": "https://example.test/show",
        "media_type": kind,
        "tmdb_id": 42,
        "source": "sto",
    }
    monkeypatch.setattr(seerr_processing.tmdb, "identity_details", lambda *a: value)
    monkeypatch.setattr(seerr_processing, "candidates", lambda *a: [hit])
    monkeypatch.setattr(
        seerr_processing.seerr_matching, "enrich_candidate", lambda *a: dict(hit)
    )
    rows = [
        {
            "url": f"https://example.test/s{s}e1",
            "season_number": s,
            "episode_number": 1,
            "episode_title": f"Pilot {s}",
        }
        for s in (1, 2)
    ]
    monkeypatch.setattr(seerr_processing, "episode_rows", lambda *a: rows)
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda tid, season: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": season,
                    "episode_number": 1,
                    "name": f"Pilot {season}",
                }
            ]
        },
    )
    calls = []

    def build(url, *args):
        calls.append(url)
        return SimpleNamespace(name="Fake"), Episode(kind, 2 if "s2" in url else 1, 1)

    monkeypatch.setattr(worker, "_build_episode", build)
    secret = "integration-test-secret-with-32-characters"
    monkeypatch.setenv(seerr.SECRET, secret)
    payload = {
        "notification_type": "MEDIA_APPROVED",
        "request_id": 401,
        "media_type": kind,
        "media_tmdbid": 42,
        "media_imdbid": "tt1234567",
        "seasons": [1, 2] if kind == "tv" else [],
    }
    response = auth_client.post(
        "/api/integrations/seerr/webhook",
        json=payload,
        headers={"Authorization": "Bearer " + secret},
    )
    assert response.status_code == 202
    qid = response.json["queue_id"]
    item = db.claim_next_queued()
    assert item["id"] == qid
    worker._process(item)
    assert db.get_queue_item(qid)["status"] == "completed"
    assert seerr.get_request(request_id=401)["status"] == "completed"
    if kind == "tv":
        assert sorted(p.name for p in setup_media[1].iterdir()) == ["Example"]
        for season in (1, 2):
            assert (
                setup_media[1]
                / "Example"
                / f"Season {season:02d}"
                / f"Example S{season:02d}E01.mp4"
            ).read_bytes() == b"finished-media"
        assert not list(setup_media[0].iterdir())
    else:
        assert (setup_media[0] / "Example (2020).mp4").read_bytes() == b"finished-media"
        assert not list(setup_media[1].iterdir())
    again = auth_client.post(
        "/api/integrations/seerr/webhook",
        json=payload,
        headers={"Authorization": secret},
    )
    assert again.status_code == 200 and again.json["duplicate"]
    assert db.claim_next_queued() is None
    assert len(calls) == (2 if kind == "tv" else 1)


def test_missing_episode_joins_nfo_identified_existing_folder(setup_media, monkeypatch):
    qid = request(monkeypatch, "tv", [1])
    show = setup_media[1] / "Existing localized title"
    season = show / "Season 01"
    season.mkdir(parents=True)
    (show / "tvshow.nfo").write_text("<tvshow><tmdbid>42</tmdbid></tvshow>")
    first = season / "Existing localized title S01E01.mp4"
    first.write_bytes(b"original episode")
    first.with_suffix(".nfo").write_text(
        "<episodedetails><season>1</season><episode>1</episode></episodedetails>"
    )
    original_time = first.stat().st_mtime_ns
    monkeypatch.setattr(
        seerr_processing,
        "episode_rows",
        lambda *a: [
            {
                "url": "https://example.test/ep2",
                "season_number": 1,
                "episode_number": 2,
                "episode_title": "Second",
            }
        ],
    )
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": n,
                    "name": name,
                }
                for n, name in ((1, "First"), (2, "Second"))
            ]
        },
    )
    calls = []

    def build(url, *args):
        calls.append(url)
        return SimpleNamespace(name="Fake"), Episode("tv", 1, 2)

    monkeypatch.setattr(worker, "_build_episode", build)
    worker._process(db.get_queue_item(qid))
    assert calls == ["https://example.test/ep2"]
    assert (
        season / "Existing localized title S01E02.mp4"
    ).read_bytes() == b"finished-media"
    assert first.read_bytes() == b"original episode"
    assert first.stat().st_mtime_ns == original_time
    assert sorted(p.name for p in setup_media[1].iterdir()) == [
        "Existing localized title"
    ]
    assert seerr.get_request(request_id=1)["status"] == "completed"


@pytest.mark.parametrize("kind", ["movie", "tv"])
def test_missing_destination_pauses_before_provider_download(
    setup_media, monkeypatch, kind
):
    qid = request(monkeypatch, kind, [1] if kind == "tv" else [])
    setup_media[0 if kind == "movie" else 1].rmdir()
    monkeypatch.setattr(
        worker,
        "_build_episode",
        lambda *a: pytest.fail("No download with absent mount"),
    )
    worker._process(db.get_queue_item(qid))
    assert seerr.get_request(request_id=1)["status"] == "manual_review"
    assert db.get_queue_item(qid)["pause_requested"] == 1


def test_changed_identity_before_download_requires_manual_review(
    setup_media, monkeypatch
):
    qid = request(monkeypatch)
    monkeypatch.setattr(
        seerr_processing.seerr_matching,
        "enrich_candidate",
        lambda *a: {"media_type": "movie", "tmdb_id": 999},
    )
    episode = Episode()
    monkeypatch.setattr(
        episode, "download", lambda: pytest.fail("Identity conflict must stop bytes")
    )
    monkeypatch.setattr(
        worker, "_build_episode", lambda *a: (SimpleNamespace(name="Fake"), episode)
    )
    worker._process(db.get_queue_item(qid))
    row = seerr.get_request(request_id=1)
    assert row["status"] == "manual_review"
    assert row["identity_status"] == "manual_review"
    assert "identity conflicts" in row["error"]
    item = db.get_queue_item(qid)
    assert item["status"] == "queued" and item["pause_requested"] == 1
    assert item["current_episode"] == 0
    assert not list(setup_media[0].iterdir())


def test_webhook_descriptive_metadata_never_replaces_verified_identity(
    setup_media, monkeypatch
):
    qid = request(monkeypatch)
    request_row = seerr.get_request(request_id=1)
    incoming = {
        **request_row["payload"],
        "original_title": "Unverified title",
        "release_year": 1999,
        "release_date": "1999-01-01",
    }
    with db.session() as conn:
        conn.execute(
            "UPDATE seerr_requests SET payload=? WHERE request_id=1",
            (json.dumps(incoming),),
        )
    monkeypatch.setattr(
        worker, "_build_episode", lambda *a: (SimpleNamespace(name="Fake"), Episode())
    )
    worker._process(db.get_queue_item(qid))
    row = seerr.get_request(request_id=1)
    assert row["identity"]["original_title"] == "Example"
    assert row["identity"]["release_year"] == 2020
    assert row["payload"]["original_title"] == "Unverified title"
    assert (setup_media[0] / "Example (2020).mp4").is_file()


@pytest.mark.parametrize("change", ["title", "series", "missing"])
def test_source_episode_changed_after_prepare_never_downloads(
    setup_media, monkeypatch, change
):
    qid = request(monkeypatch, "tv", [1])
    calls = []
    source = {
        "url": "https://example.test/ep",
        "season_number": 1,
        "episode_number": 1,
        "episode_title": "First",
    }

    def rows(*args):
        calls.append(1)
        if len(calls) == 1:
            return [dict(source)]
        if change == "missing":
            return []
        return [
            {
                **source,
                **(
                    {"episode_title": "Different episode"}
                    if change == "title"
                    else {"series_tmdb_id": 999}
                ),
            }
        ]

    monkeypatch.setattr(seerr_processing, "episode_rows", rows)
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": 1,
                    "name": "First",
                }
            ]
        },
    )
    episode = Episode("tv", 1, 1)
    monkeypatch.setattr(
        episode, "download", lambda: pytest.fail("Changed episode must not download")
    )
    monkeypatch.setattr(
        worker, "_build_episode", lambda *a: (SimpleNamespace(name="Fake"), episode)
    )
    worker._process(db.get_queue_item(qid))
    assert len(calls) == 2
    assert seerr.get_request(request_id=1)["status"] == "manual_review"
    assert db.get_queue_item(qid)["current_episode"] == 0
    assert not list(setup_media[1].iterdir())


def test_provider_search_retry_is_bounded_without_downloading_partial_results(
    setup_media, monkeypatch
):
    qid = request(monkeypatch)

    def unavailable(*args):
        raise seerr_processing.ProviderSearchUnavailable("Provider search failed")

    monkeypatch.setattr(seerr_processing, "candidates", unavailable)
    monkeypatch.setattr(
        worker,
        "_build_episode",
        lambda *a: pytest.fail("Incomplete search must not download"),
    )
    monkeypatch.setattr(seerr_processing.time, "time", lambda: 1000.0)
    for count, delay in enumerate((30, 120, 600), 1):
        db.set_queue_status(qid, "running")
        worker._process(db.get_queue_item(qid))
        row = seerr.get_request(request_id=1)
        assert row["status"] == "waiting_retry"
        assert row["identity_status"] == "provider_retry"
        assert row["retry_count"] == count
        assert db.get_queue_item(qid)["not_before"] == 1000.0 + delay
    db.set_queue_status(qid, "running")
    worker._process(db.get_queue_item(qid))
    assert seerr.get_request(request_id=1)["status"] == "manual_review"
    assert seerr.get_request(request_id=1)["identity_status"] == "provider_failed"
    assert db.get_queue_item(qid)["pause_requested"] == 1
    assert not list(setup_media[0].iterdir())


def test_review_transaction_rolls_back_queue_when_request_update_fails(
    setup_media, monkeypatch
):
    import sqlite3

    qid = request(monkeypatch)
    db.set_queue_status(qid, "running")
    before = db.get_queue_item(qid)
    prior = seerr.get_request(request_id=1)
    with db.session() as conn:
        conn.execute(
            "CREATE TRIGGER fail_review BEFORE UPDATE ON seerr_requests "
            "WHEN NEW.status='manual_review' BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected storage failure"):
        seerr_processing.review(before, "Needs review")
    assert db.get_queue_item(qid) == before
    assert seerr.get_request(request_id=1) == prior


def test_review_does_not_overwrite_cancel_between_precheck_and_transaction(
    setup_media, monkeypatch
):
    qid = request(monkeypatch)
    db.set_queue_status(qid, "running")
    original = seerr_processing.stopped
    checks = []

    def stopped(item):
        checks.append(1)
        if len(checks) == 1:
            db.cancel_queue_item(qid)
            return False
        return original(item)

    monkeypatch.setattr(seerr_processing, "stopped", stopped)
    assert not seerr_processing.review(db.get_queue_item(qid), "Needs review")
    assert db.get_queue_item(qid)["status"] == "cancelled"
    assert seerr.get_request(request_id=1)["status"] == "cancelled"


@pytest.mark.parametrize("partial", [False, True])
def test_finish_transaction_rolls_back_both_states_on_storage_failure(
    setup_media, monkeypatch, partial
):
    import sqlite3

    qid = request(monkeypatch)
    db.set_queue_status(qid, "running")
    seerr.update(
        1,
        status="downloading",
        episode_review={"pending": [{"episode_number": 2}]} if partial else {},
    )
    before_queue, before_request = (
        db.get_queue_item(qid),
        seerr.get_request(request_id=1),
    )
    with db.session() as conn:
        conn.execute(
            "CREATE TRIGGER fail_finish BEFORE UPDATE ON seerr_requests "
            "WHEN NEW.status IN ('completed','partial') BEGIN SELECT RAISE(ABORT, 'injected finish failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected finish failure"):
        seerr_files.finish(before_queue, "completed")
    assert db.get_queue_item(qid) == before_queue
    assert seerr.get_request(request_id=1) == before_request


@pytest.mark.parametrize("completed", [False, True])
def test_outer_worker_failure_synchronizes_without_undoing_completion(
    setup_media, monkeypatch, caplog, completed
):
    qid = request(monkeypatch)
    seerr.update(1, status="downloading")
    secret = "outer-worker-exception-secret"

    def process(item):
        if completed:
            seerr_files.finish(item, "completed")
        raise RuntimeError(secret)

    class EndLoop(BaseException):
        pass

    monkeypatch.setattr(worker, "_process", process)
    monkeypatch.setattr(
        worker.time, "sleep", lambda *a: (_ for _ in ()).throw(EndLoop())
    )
    with pytest.raises(EndLoop):
        worker._run_owned()
    expected = "completed" if completed else "failed"
    assert db.get_queue_item(qid)["status"] == expected
    assert seerr.get_request(request_id=1)["status"] == expected
    assert secret not in caplog.text
    assert secret not in str(seerr.get_request(request_id=1))


@pytest.mark.parametrize("status", ["checking", "downloading"])
@pytest.mark.parametrize("paused", [False, True])
def test_restart_synchronizes_interrupted_request_status(
    setup_media, monkeypatch, status, paused
):
    qid = request(monkeypatch)
    db.set_queue_status(qid, "running")
    seerr.update(1, status=status)
    with db.session() as conn:
        conn.execute(
            "UPDATE download_queue SET pause_requested=? WHERE id=?", (int(paused), qid)
        )
    assert db.reset_stale_running()
    assert seerr.get_request(request_id=1)["status"] == (
        "paused" if paused else "waiting"
    )
    assert db.get_queue_item(qid)["status"] == "queued"
    assert bool(db.get_queue_item(qid)["pause_requested"]) == paused
    if paused:
        assert db.claim_next_queued() is None
    else:
        assert db.claim_next_queued()["id"] == qid


@pytest.mark.parametrize(
    "status,queue_status,paused",
    [
        ("completed", "completed", False),
        ("already_present", "completed", False),
        ("manual_review", "queued", True),
        ("partial", "queued", True),
        ("waiting_retry", "queued", False),
        ("failed", "failed", False),
    ],
)
def test_restart_preserves_stopped_and_backoff_request_states(
    setup_media, monkeypatch, status, queue_status, paused
):
    qid = request(monkeypatch)
    db.set_queue_status(qid, queue_status)
    seerr.update(1, status=status, retry_count=2)
    with db.session() as conn:
        conn.execute(
            "UPDATE download_queue SET pause_requested=?,not_before=9999999999 WHERE id=?",
            (int(paused), qid),
        )
    before = seerr.get_request(request_id=1)
    assert db.reset_stale_running()
    assert seerr.get_request(request_id=1) == before
    queue = db.get_queue_item(qid)
    assert queue["status"] == queue_status
    assert bool(queue["pause_requested"]) == paused
    assert queue["not_before"] == 9999999999


def test_explicit_specials_use_season_zero_without_normal_seasons(
    setup_media, monkeypatch
):
    qid = request(monkeypatch, "tv", [0])
    value = identity("tv")
    value["seasons"] = [{"season_number": 0}, {"season_number": 1}]
    monkeypatch.setattr(seerr_processing.tmdb, "identity_details", lambda *a: value)
    row = {
        "url": "https://example.test/special",
        "season_number": 0,
        "episode_number": 1,
        "episode_title": "Special",
    }
    monkeypatch.setattr(seerr_processing, "episode_rows", lambda *a: [dict(row)])
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda tid, season: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": season,
                    "episode_number": 1,
                    "name": "Special",
                }
            ]
        },
    )
    monkeypatch.setattr(
        worker,
        "_build_episode",
        lambda *a: (SimpleNamespace(name="Fake"), Episode("tv", 0, 1)),
    )
    worker._process(db.get_queue_item(qid))
    assert (
        setup_media[1] / "Example/Season 00/Example S00E01.mp4"
    ).read_bytes() == b"finished-media"
    assert not (setup_media[1] / "Example/Season 01").exists()
    assert seerr.get_request(request_id=1)["status"] == "completed"


def test_target_change_keeps_existing_receipt_and_downloads_only_missing_to_new_mount(
    setup_media, monkeypatch, tmp_path
):
    qid = request(monkeypatch, "tv", [1])
    old_entry = {"_seerr_identity": identity("tv"), "season": 1, "episode": 1}
    _, old = seerr_files.output(old_entry, "mp4")
    old.parent.mkdir(parents=True)
    old.write_bytes(b"original on old mount")
    seerr_files.record(old_entry, old)
    old_time = old.stat().st_mtime_ns
    new = tmp_path / "new-shows"
    new.mkdir()
    target = db.add_custom_path("New shows", str(new))
    settings = media_targets.read()
    media_targets.save(settings["movie"], target, settings["revision"])
    row = {
        "url": "https://example.test/ep2",
        "season_number": 1,
        "episode_number": 2,
        "episode_title": "Second",
    }
    monkeypatch.setattr(seerr_processing, "episode_rows", lambda *a: [dict(row)])
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": n,
                    "name": name,
                }
                for n, name in ((1, "First"), (2, "Second"))
            ]
        },
    )
    calls = []

    def build(url, *args):
        calls.append(url)
        return SimpleNamespace(name="Fake"), Episode("tv", 1, 2)

    monkeypatch.setattr(worker, "_build_episode", build)
    worker._process(db.get_queue_item(qid))
    assert calls == [row["url"]]
    assert (
        old.read_bytes() == b"original on old mount"
        and old.stat().st_mtime_ns == old_time
    )
    assert (
        new / "Example/Season 01/Example S01E02.mp4"
    ).read_bytes() == b"finished-media"
    assert not (new / "Example/Season 01/Example S01E01.mp4").exists()
    assert not (old.parent / "Example S01E02.mp4").exists()
    assert seerr.get_request(request_id=1)["status"] == "completed"


def test_destination_change_during_transfer_pins_current_file_only(
    setup_media, monkeypatch, tmp_path
):
    qid = request(monkeypatch, "tv", [1])
    new = tmp_path / "next-shows"
    new.mkdir()
    target = db.add_custom_path("Next shows", str(new))
    rows = [
        {
            "url": f"https://example.test/ep{n}",
            "season_number": 1,
            "episode_number": n,
            "episode_title": name,
        }
        for n, name in ((1, "First"), (2, "Second"))
    ]
    monkeypatch.setattr(seerr_processing, "episode_rows", lambda *a: rows)
    monkeypatch.setattr(
        seerr_processing.tmdb,
        "season_details",
        lambda *a: {
            "episodes": [
                {
                    "series_tmdb_id": 42,
                    "season_number": 1,
                    "episode_number": row["episode_number"],
                    "name": row["episode_title"],
                }
                for row in rows
            ]
        },
    )
    paths = []

    class SwitchingEpisode(Episode):
        def download(self):
            before = self._episode_path
            if self.video_identity["episode"] == 1:
                settings = media_targets.read()
                media_targets.save(settings["movie"], target, settings["revision"])
            assert self._episode_path == before
            paths.append(before)
            super().download()

    monkeypatch.setattr(
        worker,
        "_build_episode",
        lambda url, *a: (
            SimpleNamespace(name="Fake"),
            SwitchingEpisode("tv", 1, int(url[-1])),
        ),
    )
    worker._process(db.get_queue_item(qid))
    expected = [
        setup_media[1] / "Example/Season 01/Example S01E01.mp4",
        new / "Example/Season 01/Example S01E02.mp4",
    ]
    assert paths == expected
    for n, path in enumerate(expected, 1):
        assert path.read_bytes() == b"finished-media"
        assert (
            seerr_files.existing(
                {"_seerr_identity": identity("tv"), "season": 1, "episode": n}
            )
            == path
        )
    assert not (new / "Example/Season 01/Example S01E01.mp4").exists()
    assert not (setup_media[1] / "Example/Season 01/Example S01E02.mp4").exists()
    assert seerr.get_request(request_id=1)["status"] == "completed"
    assert db.get_queue_item(qid)["status"] == "completed"


def test_worker_recovers_when_failure_status_write_also_fails(
    setup_media, monkeypatch, queue_item, caplog
):
    import sqlite3

    first = request(monkeypatch)
    second = queue_item()
    attempted = []
    original_finish = seerr_files.finish
    finish_calls = []

    class StopWorker(BaseException):
        pass

    def finish(item, status, *args, **kwargs):
        finish_calls.append((item["id"], status))
        if len(finish_calls) == 1:
            raise sqlite3.OperationalError("temporary storage failure")
        return original_finish(item, status, *args, **kwargs)

    def process(item):
        attempted.append(item["id"])
        if len(attempted) == 1:
            raise RuntimeError("temporary processing failure signed-secret-do-not-log")
        if item["id"] == first:
            original_finish(item, "completed")
        else:
            db.set_queue_status(item["id"], "completed")
            raise StopWorker()

    sleeps = []

    def sleep(delay):
        sleeps.append(delay)
        if len(sleeps) > 5:
            pytest.fail("Running row stranded the queue after database recovery")

    monkeypatch.setattr(worker, "_process", process)
    monkeypatch.setattr(seerr_files, "finish", finish)
    monkeypatch.setattr(worker.time, "sleep", sleep)
    with pytest.raises(StopWorker):
        worker._run()
    assert attempted == [first, first, second]
    assert db.get_queue_item(first)["status"] == "completed"
    assert db.get_queue_item(second)["status"] == "completed"
    assert seerr.get_request(queue_id=first)["status"] == "completed"
    assert sleeps == [worker.IDLE_SECONDS]
    assert "signed-secret-do-not-log" not in caplog.text


def test_transfer_pause_synchronizes_request(setup_media, monkeypatch):
    from h0melab.models.common.common import DownloadPaused

    qid = request(monkeypatch)
    episode = Episode()

    def download():
        db.pause_queue_item(qid)
        raise DownloadPaused()

    monkeypatch.setattr(episode, "download", download)
    monkeypatch.setattr(
        worker, "_build_episode", lambda *a: (SimpleNamespace(name="Fake"), episode)
    )
    item = db.claim_next_queued()
    assert item["id"] == qid
    worker._process(item)
    assert db.get_queue_item(qid)["pause_requested"] == 1
    assert db.get_queue_item(qid)["status"] == "queued"
    assert seerr.get_request(queue_id=qid)["status"] == "paused"


def test_pause_request_write_failure_rolls_back_queue(setup_media, monkeypatch):
    import sqlite3

    qid = request(monkeypatch)
    db.set_queue_status(qid, "running")
    seerr.update(1, status="downloading")
    with db.session() as conn:
        conn.execute(
            "CREATE TRIGGER reject_pause BEFORE UPDATE OF status ON seerr_requests "
            "WHEN NEW.status='paused' BEGIN SELECT RAISE(ABORT, 'injected pause failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected pause failure"):
        db.finish_pause(qid)
    assert db.get_queue_item(qid)["status"] == "running"
    assert db.get_queue_item(qid)["pause_requested"] == 0
    assert seerr.get_request(queue_id=qid)["status"] == "downloading"


def test_resume_updates_paused_request_atomically(setup_media, monkeypatch):
    import sqlite3

    qid = request(monkeypatch)
    db.finish_pause(qid)
    with db.session() as conn:
        conn.execute(
            "CREATE TRIGGER reject_resume BEFORE UPDATE OF status ON seerr_requests "
            "WHEN NEW.status='waiting' BEGIN SELECT RAISE(ABORT, 'injected resume failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected resume failure"):
        db.resume_queue_item(qid)
    assert db.get_queue_item(qid)["pause_requested"] == 1
    assert seerr.get_request(queue_id=qid)["status"] == "paused"
    with db.session() as conn:
        conn.execute("DROP TRIGGER reject_resume")
    assert db.resume_queue_item(qid) == (True, None)
    assert db.get_queue_item(qid)["pause_requested"] == 0
    assert seerr.get_request(queue_id=qid)["status"] == "waiting"
    assert db.claim_next_queued()["id"] == qid


@pytest.mark.parametrize("status", ["manual_review", "partial", "waiting_retry"])
def test_generic_resume_preserves_review_state(setup_media, monkeypatch, status):
    qid = request(monkeypatch)
    db.finish_pause(qid)
    seerr.update(1, status=status)
    assert db.resume_queue_item(qid) == (True, None)
    assert seerr.get_request(queue_id=qid)["status"] == status


@pytest.mark.parametrize("running,force", [(False, False), (False, True), (True, True)])
def test_immediate_cancel_synchronizes_request_atomically(
    setup_media, monkeypatch, running, force
):
    import sqlite3

    qid = request(monkeypatch)
    if running:
        db.set_queue_status(qid, "running")
    seerr.update(1, status="downloading" if running else "waiting")
    before = db.get_queue_item(qid)
    with db.session() as conn:
        conn.execute(
            "CREATE TRIGGER reject_cancel BEFORE UPDATE OF status ON seerr_requests "
            "WHEN NEW.status='cancelled' BEGIN SELECT RAISE(ABORT, 'injected cancel failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected cancel failure"):
        db.cancel_queue_item(qid, force=force)
    assert db.get_queue_item(qid) == before
    assert seerr.get_request(queue_id=qid)["status"] == (
        "downloading" if running else "waiting"
    )
    with db.session() as conn:
        conn.execute("DROP TRIGGER reject_cancel")
    assert db.cancel_queue_item(qid, force=force) == (True, None)
    assert db.get_queue_item(qid)["status"] == "cancelled"
    assert seerr.get_request(queue_id=qid)["status"] == "cancelled"


def test_graceful_running_cancel_waits_for_worker(setup_media, monkeypatch):
    qid = request(monkeypatch)
    db.set_queue_status(qid, "running")
    seerr.update(1, status="downloading")
    assert db.cancel_queue_item(qid) == (True, None)
    assert db.get_queue_item(qid)["status"] == "running"
    assert db.get_queue_item(qid)["cancel_requested"] == 1
    assert seerr.get_request(queue_id=qid)["status"] == "downloading"


@pytest.mark.parametrize(
    "state", ["cancelled", "completed", "failed", "graceful_cancel"]
)
def test_late_pause_cannot_resurrect_stopped_request(setup_media, monkeypatch, state):
    qid = request(monkeypatch)
    if state == "graceful_cancel":
        db.set_queue_status(qid, "running")
        seerr.update(1, status="downloading")
        db.cancel_queue_item(qid)
    else:
        db.set_queue_status(qid, state)
        seerr.update(1, status=state)
    before = db.get_queue_item(qid)
    request_before = seerr.get_request(queue_id=qid)
    db.finish_pause(qid)
    assert db.get_queue_item(qid) == before
    assert seerr.get_request(queue_id=qid) == request_before


@pytest.mark.parametrize(
    "status,partial", [("completed", False), ("completed", True), ("failed", False)]
)
def test_late_finish_preserves_force_cancel(setup_media, monkeypatch, status, partial):
    qid = request(monkeypatch)
    db.set_queue_status(qid, "running")
    seerr.update(
        1, status="downloading", episode_review={"pending": [{}]} if partial else {}
    )
    stale = db.get_queue_item(qid)
    assert db.cancel_queue_item(qid, force=True) == (True, None)
    notices = []
    monkeypatch.setattr(
        seerr, "notify", lambda rid, event, title: notices.append(event)
    )
    seerr_files.finish(stale, status)
    current = db.get_queue_item(qid)
    assert current["status"] == "cancelled"
    assert current["force_cancelled"] == 1
    assert current["pause_requested"] == 0
    assert seerr.get_request(queue_id=qid)["status"] == "cancelled"
    assert notices == ["cancelled"]


def test_prepare_checks_manual_evidence_before_replacing_candidates(
    setup_media, monkeypatch
):
    qid = request(monkeypatch)
    old = {
        "url": "https://example.test/show",
        "media_type": "movie",
        "title": "Reviewed localized title",
    }
    seerr.update(
        1,
        selected_url=old["url"],
        candidates=[old],
        review_reason="Verified this title manually",
    )
    inspected = []

    def revalidate(identity, prior):
        inspected.append(prior["candidates"])
        raise seerr_processing.seerr_matching.IdentityReviewRequired(
            "Reviewed page changed"
        )

    monkeypatch.setattr(
        seerr_processing.seerr_matching, "revalidate_selection", revalidate
    )
    assert not seerr_processing.prepare(db.get_queue_item(qid))
    assert inspected == [[old]]
    current = seerr.get_request(request_id=1)
    assert current["candidates"] != [old]
    assert current["selected_url"] == ""
    assert current["review_reason"] == ""
    assert current["status"] == "manual_review"
    assert db.get_queue_item(qid)["pause_requested"] == 1


def test_manual_selection_changed_title_is_stopped_by_real_revalidation(
    setup_media, monkeypatch
):
    from h0melab import providers
    from h0melab.web import seerr_matching

    original_enrich = seerr_matching.enrich_candidate
    qid = request(monkeypatch)
    old = {
        "url": "https://example.test/show",
        "media_type": "movie",
        "title": "Reviewed localized title",
    }
    seerr.update(
        1,
        selected_url=old["url"],
        candidates=[old],
        review_reason="Checked the localized title",
    )
    changed = {"url": old["url"], "media_type": "movie", "title": "Different film"}
    monkeypatch.setattr(seerr_processing, "candidates", lambda _: [changed])
    monkeypatch.setattr(seerr_matching, "enrich_candidate", original_enrich)
    monkeypatch.setattr(
        providers,
        "resolve_provider",
        lambda url: SimpleNamespace(
            series_cls=lambda **kw: SimpleNamespace(
                media_type="movie", title="Different film"
            )
        ),
    )
    monkeypatch.setattr(
        worker,
        "_build_episode",
        lambda *a: pytest.fail("Changed reviewed page must not download"),
    )
    worker._process(db.get_queue_item(qid))
    current = seerr.get_request(request_id=1)
    assert current["status"] == "manual_review"
    assert current["candidates"] != [old]
    assert current["selected_url"] == ""
    assert current["review_reason"] == ""
    assert db.get_queue_item(qid)["pause_requested"] == 1


@pytest.mark.parametrize("restore", [True, False])
def test_changed_review_can_be_approved_again_on_same_queue(
    setup_media, monkeypatch, restore
):
    from h0melab import providers
    from h0melab.web import seerr_matching

    original_enrich = seerr_matching.enrich_candidate
    qid = request(monkeypatch)
    url = "https://example.test/show"
    old = {"url": url, "media_type": "movie", "title": "Reviewed localized title"}
    seerr.update(
        1,
        selected_url=url,
        candidates=[old],
        review_reason="Checked this localized title",
    )
    model = SimpleNamespace(media_type="movie", title="Changed page")
    monkeypatch.setattr(seerr_matching, "enrich_candidate", original_enrich)
    monkeypatch.setattr(
        providers,
        "resolve_provider",
        lambda url: SimpleNamespace(series_cls=lambda **kw: model),
    )
    monkeypatch.setattr(
        seerr_processing,
        "candidates",
        lambda _: [{"url": url, "media_type": "movie", "title": model.title}],
    )
    calls = []

    def build(*args):
        calls.append(url)
        return SimpleNamespace(name="Fake"), Episode()

    monkeypatch.setattr(worker, "_build_episode", build)
    worker._process(db.get_queue_item(qid))
    assert calls == []
    assert seerr.get_request(request_id=1)["status"] == "manual_review"
    if restore:
        model.title = old["title"]
    assert seerr.retry_request(1) == qid
    worker._process(db.get_queue_item(qid))
    assert calls == []
    assert seerr.get_request(request_id=1)["status"] == "manual_review"
    assert (
        seerr.review_request(
            1,
            {
                "selected_url": url,
                "reason": "Checked the current localized title again",
            },
        )
        == qid
    )
    worker._process(db.get_queue_item(qid))
    assert calls == [url]
    assert (setup_media[0] / "Example (2020).mp4").read_bytes() == b"finished-media"
    assert seerr.get_request(request_id=1)["status"] == "completed"
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM download_queue").fetchone()[0] == 1


@pytest.mark.parametrize("kind", ["movie", "tv"])
def test_jellyfin_refresh_only_after_new_completed_file(setup_media, monkeypatch, kind):
    from h0melab.web import jellyfin

    qid = request(monkeypatch, kind, [1] if kind == "tv" else None)
    if kind == "tv":
        monkeypatch.setattr(
            seerr_processing,
            "episode_rows",
            lambda *a: [
                {
                    "url": "https://example.test/episode",
                    "season_number": 1,
                    "episode_number": 1,
                    "episode_title": "Pilot",
                    "series_tmdb_id": 42,
                }
            ],
        )
        monkeypatch.setattr(
            seerr_processing.tmdb,
            "season_details",
            lambda *a: {
                "episodes": [
                    {
                        "series_tmdb_id": 42,
                        "season_number": 1,
                        "episode_number": 1,
                        "name": "Pilot",
                    }
                ]
            },
        )
    calls = []
    monkeypatch.setattr(
        jellyfin, "refresh", lambda media_type: calls.append(media_type)
    )
    monkeypatch.setattr(
        worker,
        "_build_episode",
        lambda *a: (
            SimpleNamespace(name="Fake"),
            Episode(kind, 1 if kind == "tv" else 0, 1 if kind == "tv" else 0),
        ),
    )
    worker._process(db.get_queue_item(qid))
    assert calls == [kind]
    assert seerr.get_request(queue_id=qid)["status"] == "completed"
    worker._process(db.get_queue_item(qid))
    assert calls == [kind]
