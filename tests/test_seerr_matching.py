import pytest

from h0melab.web import seerr_matching as matching


@pytest.fixture
def identity():
    return {
        "media_type": "movie",
        "tmdb_id": 157336,
        "imdb_id": "tt0816692",
        "title": "Interstellar",
        "release_year": 2014,
    }


def test_title_alone_is_not_evidence(identity):
    assert not matching.assess_candidate(
        identity, {"media_type": "movie", "title": "Interstellar"}
    )["eligible"]


def test_exact_title_and_year(identity):
    assert matching.assess_candidate(
        identity, {"media_type": "movie", "title": "Interstellar", "year": 2014}
    )["eligible"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("tmdb_id", 1),
        ("imdb_id", "tt0773262"),
        ("media_type", "tv"),
        ("year", 2015),
        ("tmdb_id", True),
    ],
)
def test_conflicts_override_title_score(identity, field, value):
    hit = {
        "media_type": "movie",
        "title": "Interstellar",
        "year": 2014,
        "tmdb_id": 157336,
        "imdb_id": "tt0816692",
    }
    hit[field] = value
    result = matching.assess_candidate(identity, hit)
    assert result["conflict"] and not result["eligible"]


def test_id_still_requires_type(identity):
    assert not matching.assess_candidate(identity, {"tmdb_id": 157336})["eligible"]


def test_multiple_matching_sources_choose_first_verified(identity):
    hits = [
        {"media_type": "movie", "tmdb_id": 157336, "url": url}
        for url in ("https://one", "https://two")
    ]
    assert matching.choose_candidate(identity, hits) == hits[0]
    assert matching.choose_candidate(identity, [hits[0], hits[0]]) == hits[0]


def test_no_fuzzy_alias_or_remake_selection(identity):
    assert not matching.assess_candidate(
        identity, {"media_type": "movie", "title": "Interstellar 2", "year": 2014}
    )["eligible"]
    assert not matching.assess_candidate(
        identity, {"media_type": "movie", "title": "Interstellar", "year": 2024}
    )["eligible"]


def test_metadata_failure_is_not_ignored(identity):
    assert not matching.assess_candidate(
        identity,
        {"media_type": "movie", "tmdb_id": 157336, "metadata_error": "timeout"},
    )["eligible"]


@pytest.mark.parametrize(
    "change",
    [
        {"season_number": 2},
        {"episode_number": 2},
        {"series_tmdb_id": 2},
        {"title_en": "Another story"},
        {"episode_number": True},
    ],
)
def test_episode_contradiction(change):
    identity = {"tmdb_id": 1405, "imdb_id": "tt0773262"}
    episode = {
        "series_tmdb_id": 1405,
        "season_number": 1,
        "episode_number": 1,
        "name": "Dexter",
    }
    row = {"season_number": 1, "episode_number": 1, "title_en": "Dexter"}
    row.update(change)
    assert not matching.assess_episode(identity, episode, row)["eligible"]


def test_episode_mapping_needs_real_evidence():
    identity = {"tmdb_id": 1405}
    episode = {
        "series_tmdb_id": 1405,
        "season_number": 1,
        "episode_number": 1,
        "name": "Dexter",
    }
    row = {"season_number": 1, "episode_number": 1, "title_en": "Episode 1"}
    assert not matching.assess_episode(identity, episode, row)["eligible"]
    row["title_en"] = "Dexter"
    assert matching.assess_episode(identity, episode, row)["eligible"]


def _provider(monkeypatch, **metadata):
    from types import SimpleNamespace

    model = SimpleNamespace(**metadata)
    provider = SimpleNamespace(series_cls=lambda **kwargs: model, episode_cls=None)
    monkeypatch.setattr("h0melab.providers.resolve_provider", lambda url: provider)


def test_enrichment_reads_model_ids_type_and_year(monkeypatch, identity):
    _provider(
        monkeypatch,
        title="Interstellar",
        is_movie=True,
        release_year=2014,
        imdb="tt0816692",
    )
    hit = matching.enrich_candidate(
        {"url": "https://source/title", "title": "Interstellar"}
    )
    assert hit["imdb_id"] == "tt0816692"
    assert hit["media_type"] == "movie"
    assert matching.assess_candidate(identity, hit)["eligible"]


@pytest.mark.parametrize(
    "field,old",
    [("imdb_id", "tt0000001"), ("media_type", "tv"), ("release_year", 1999)],
)
def test_enrichment_preserves_conflicting_evidence(monkeypatch, identity, field, old):
    _provider(
        monkeypatch,
        title="Interstellar",
        is_movie=True,
        release_year=2014,
        imdb="tt0816692",
    )
    original = dict(url="https://source/title", **{field: old})
    hit = matching.enrich_candidate(original)
    assert hit[field] == old
    assert hit["metadata_conflicts"]
    assert matching.assess_candidate(identity, hit)["conflict"]
    assert original == dict(url="https://source/title", **{field: old})


def test_enrichment_does_not_guess_type(monkeypatch, identity):
    _provider(monkeypatch, title="Interstellar", release_year=2014, imdb="tt0816692")
    hit = matching.enrich_candidate({"url": "https://source/movie/157336"})
    assert not hit.get("media_type")
    assert not matching.assess_candidate(identity, hit)["eligible"]


def test_enrichment_series_range_is_premiere(monkeypatch):
    _provider(monkeypatch, title="Dexter", is_series=True, release_year="2006-2013")
    hit = matching.enrich_candidate({"url": "https://source/title"})
    assert hit["release_year"] == "2006"


def test_enrichment_masks_provider_failure(monkeypatch):
    def fail(url):
        raise ValueError("secret-token-in-url")

    monkeypatch.setattr("h0melab.providers.resolve_provider", fail)
    hit = matching.enrich_candidate({"url": "https://source/title"})
    assert hit["metadata_error"] == "Provider metadata could not be verified"
    assert "secret-token" not in str(hit)


def test_enrichment_checks_external_id_disagreement(monkeypatch, identity):
    _provider(monkeypatch, title="Interstellar", is_movie=True, imdb="tt0816692")
    hit = matching.enrich_candidate(
        {"url": "https://source/title", "external_ids": {"imdb_id": "tt0000001"}}
    )
    assert matching.assess_candidate(identity, hit)["conflict"]


def test_provider_search_preserves_external_identity_evidence(monkeypatch):
    from h0melab.web import sitesearch

    hit = {
        "title": "Example",
        "url": "https://megakino.example/movie",
        "media_type": "movie",
        "tmdb_id": 42,
        "imdb_id": "tt1234567",
        "external_ids": {"imdb_id": "tt1234567"},
        "release_date": "2020-01-01",
    }
    monkeypatch.setitem(sitesearch.SITE_SEARCH, "megakino", lambda _: [hit])
    found = sitesearch.search("megakino", "Example")[0]
    for field in ("tmdb_id", "imdb_id", "external_ids", "release_date"):
        assert found[field] == hit[field]


@pytest.mark.parametrize(
    "title,year",
    [
        ("Dexter: New Blood", 2021),
        ("Dexter Original Sin", 2024),
        ("Dexter", 2020),
        ("Dexterr", 2006),
    ],
)
def test_similar_tv_titles_and_remakes_are_not_automatic_matches(title, year):
    identity = {
        "media_type": "tv",
        "title": "Dexter",
        "release_year": 2006,
        "tmdb_id": 1405,
        "imdb_id": "tt0773262",
    }
    hit = {"media_type": "tv", "title": title, "year": year}
    assert not matching.assess_candidate(identity, hit)["eligible"]


def test_manually_mapped_episode_stays_bound_to_reviewed_source(monkeypatch):
    from h0melab.web import seerr_processing

    identity = {"media_type": "tv", "tmdb_id": 42, "imdb_id": "tt1234567"}
    row = {
        "url": "https://example.test/ep",
        "season_number": 3,
        "episode_number": 13,
        "episode_title": "Reviewed local title",
    }
    request = {
        "selected_url": "https://example.test/show",
        "episode_review": {
            "source": [dict(row)],
            "canonical": [
                {"season_number": 1, "episode_number": 1, "name": "Canonical"}
            ],
        },
        "episode_mapping": [
            {"url": row["url"], "season_number": 1, "episode_number": 1}
        ],
    }
    entry = {
        "url": row["url"],
        "season": 1,
        "episode": 1,
        "source_season": 3,
        "source_episode": 13,
    }
    monkeypatch.setattr(seerr_processing, "episode_rows", lambda *a: [dict(row)])
    matching.revalidate_episode_source(identity, request, entry)
    row["episode_title"] = "Replaced content"
    with pytest.raises(
        matching.IdentityReviewRequired, match="Reviewed episode title changed"
    ):
        matching.revalidate_episode_source(identity, request, entry)


@pytest.mark.parametrize(
    "value", ["2014-02-30", "20141105", "2014-11-06", "2015-11-05", True]
)
def test_release_date_contradictions_require_review(identity, value):
    identity["release_date"] = "2014-11-05"
    hit = {
        "media_type": "movie",
        "tmdb_id": identity["tmdb_id"],
        "title": "Interstellar",
        "year": 2014,
        "release_date": value,
    }
    result = matching.assess_candidate(identity, hit)
    assert result["conflict"] and not result["eligible"]


def test_matching_release_date_adds_evidence_but_not_identity_by_itself(identity):
    identity["release_date"] = "2014-11-05"
    hit = {"media_type": "movie", "release_date": "2014-11-05"}
    result = matching.assess_candidate(identity, hit)
    assert not result["eligible"] and not result["conflict"]
    assert "Release date matches" in result["reasons"]
    hit.update(title="Interstellar", year=2014)
    assert matching.assess_candidate(identity, hit)["eligible"]


def test_missing_release_date_does_not_invent_a_conflict(identity):
    identity["release_date"] = "2014-11-05"
    assert matching.assess_candidate(
        identity, {"media_type": "movie", "tmdb_id": identity["tmdb_id"]}
    )["eligible"]


def test_revalidation_keeps_search_media_type_when_detail_page_omits_it(monkeypatch):
    """SerienStream/AniWorld detail pages state no type; the search hit does."""
    from types import SimpleNamespace

    from h0melab import providers
    from h0melab.web import seerr_matching

    model = SimpleNamespace(imdb="tt0903747", title="Breaking Bad", release_year="2008")
    monkeypatch.setattr(
        providers,
        "resolve_provider",
        lambda url: SimpleNamespace(series_cls=lambda **kw: model, episode_cls=None),
    )
    identity = {
        "media_type": "tv",
        "tmdb_id": 1396,
        "imdb_id": "tt0903747",
        "title": "Breaking Bad",
        "release_year": 2008,
    }
    hit = {"url": "https://serienstream.to/serie/breaking-bad", "media_type": "series"}
    for reason in ("", seerr_matching.AUTO_REASON):
        request = {
            "selected_url": hit["url"],
            "candidates": [hit],
            "review_reason": reason,
        }
        assert (
            seerr_matching.revalidate_selection(identity, request)["media_type"]
            == "series"
        )
    # A detail page that contradicts the search type is still a hard conflict.
    model.is_movie = True
    with pytest.raises(seerr_matching.IdentityReviewRequired):
        seerr_matching.revalidate_selection(
            identity,
            {
                "selected_url": hit["url"],
                "candidates": [hit],
                "review_reason": seerr_matching.AUTO_REASON,
            },
        )
