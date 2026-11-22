"""Fresh provider details gate actual download execution, not only prepare()."""

from types import SimpleNamespace

import pytest

from h0melab import providers
from h0melab.web import db, seerr, seerr_files, seerr_matching


@pytest.fixture
def gate(monkeypatch):
    identity = {
        "media_type": "movie",
        "tmdb_id": 42,
        "imdb_id": "tt1234567",
        "title": "Example",
        "release_year": 2020,
    }
    old = {
        "url": "https://example.test/movie",
        "media_type": "movie",
        "title": "Example",
        "release_year": 2020,
    }
    model = SimpleNamespace(media_type="movie", title="Example", release_year=2020)
    monkeypatch.setattr(
        providers,
        "resolve_provider",
        lambda url: SimpleNamespace(series_cls=lambda **kw: model),
    )
    qid, _ = seerr.accept(
        {
            "request_id": 91,
            "title": "Example",
            "media_type": "movie",
            "tmdb_id": 42,
            "imdb_id": "tt1234567",
            "seasons": [],
        }
    )
    seerr.update(91, identity=identity, candidates=[old], selected_url=old["url"])
    entry = {"_seerr_identity": identity, "url": old["url"], "season": 0, "episode": 0}
    return identity, model, db.get_queue_item(qid), entry


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "Different film"),
        ("release_year", 2021),
        ("tmdb_id", 99),
        ("imdb_id", "tt9999999"),
        ("title", None),
        ("release_year", None),
    ],
)
def test_changed_or_lost_automatic_evidence_stops_before_bytes(
    gate, monkeypatch, field, value
):
    _identity, model, item, entry = gate
    setattr(model, field, value)
    episode = SimpleNamespace(
        video_identity={"media_type": "movie"},
        download=lambda: pytest.fail("No bytes before identity"),
    )
    monkeypatch.setattr(
        seerr_files, "output", lambda *a: pytest.fail("No output before identity")
    )
    with pytest.raises(ValueError, match="Fresh provider"):
        seerr_files.download(item, entry, episode)


def test_unchanged_idless_automatic_details_are_verified(gate):
    identity, _model, _item, _entry = gate
    result = seerr_matching.revalidate_selection(
        identity, seerr.get_request(request_id=91)
    )
    assert result["title"] == "Example"
    assert result["release_year"] == 2020
    assert not result.get("tmdb_id")


def test_manual_review_permits_same_noncanonical_title_but_not_changed_page(gate):
    identity, model, _item, entry = gate
    model.title = "Provider localized name"
    seerr.update(
        91,
        candidates=[{"url": entry["url"], "media_type": "movie", "title": model.title}],
        review_reason="Reviewed localized title manually",
    )
    assert (
        seerr_matching.revalidate_selection(identity, seerr.get_request(request_id=91))[
            "title"
        ]
        == model.title
    )
    model.title = "A different film now"
    with pytest.raises(ValueError, match="Reviewed provider title changed"):
        seerr_matching.revalidate_selection(identity, seerr.get_request(request_id=91))


def test_manual_review_never_overrides_fresh_id_conflict(gate):
    identity, model, _item, _entry = gate
    seerr.update(91, review_reason="Reviewed provider manually")
    model.tmdb_id = 99
    with pytest.raises(ValueError, match="Fresh provider identity conflicts"):
        seerr_matching.revalidate_selection(identity, seerr.get_request(request_id=91))


def test_changed_reviewed_release_date_requires_new_review(gate):
    identity, model, _item, entry = gate
    model.release_date = "2020-01-01"
    seerr.update(
        91,
        review_reason="Reviewed localized provider metadata",
        candidates=[
            {
                "url": entry["url"],
                "media_type": "movie",
                "title": "Example",
                "release_date": "2020-01-01",
            }
        ],
    )
    matching_request = seerr.get_request(request_id=91)
    seerr_matching.revalidate_selection(identity, matching_request)
    model.release_date = "2020-02-01"
    with pytest.raises(
        seerr_matching.IdentityReviewRequired, match="release date changed"
    ):
        seerr_matching.revalidate_selection(identity, matching_request)
