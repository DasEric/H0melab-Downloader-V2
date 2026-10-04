"""Explicit Auto-Sync subscriptions for AniWorld and SerienStream."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from h0melab.models.h0melab_to import AniworldSeason, AniworldSeries
from h0melab.web import autosync, db

AW = "https://aniworld.to/anime/stream/naruto"
STO = "https://serienstream.to/serie/dark"


class FakeSeries:
    def __init__(self, title="Naruto"):
        self.title = title


def episode(number, *, site="aniworld"):
    base = AW if site == "aniworld" else STO
    return f"{base}/staffel-1/episode-{number}"


def add_row(
    *,
    url=AW,
    site="aniworld",
    title="Naruto",
    language="German Dub",
    path_id=None,
    baseline=None,
    enabled=True,
    subtitle_language="none",
):
    row_id = db.add_autosync_series(
        series_url=url,
        site=site,
        title=title,
        language=language,
        provider="VOE",
        subtitle_language=subtitle_language,
        custom_path_id=path_id,
        baseline_episodes=baseline or [],
    )
    if not enabled:
        db.update_autosync_series(row_id, enabled=0)
    return db.get_autosync_series_item(row_id)


def inventory(count, *, site="aniworld"):
    return {episode(n, site=site): (1, n) for n in range(1, count + 1)}


def test_tracked_series_round_trip_and_duplicate_protection():
    row = add_row(baseline=["1:1"])
    assert row["series_url"] == AW
    assert json.loads(row["baseline_episodes"]) == ["1:1"]
    with pytest.raises(sqlite3.IntegrityError):
        add_row()


def test_find_tracked_series_uses_language_and_destination(tmp_path):
    default = add_row()
    path_id = db.add_custom_path("Other", str(tmp_path / "other"))
    custom = add_row(language="English Dub", path_id=path_id)

    assert db.find_autosync_series(AW, "German Dub")["id"] == default["id"]
    assert db.find_autosync_series(AW, "English Dub", path_id)["id"] == custom["id"]
    assert db.find_autosync_series(AW, "English Dub") is None
    assert db.find_autosync_series(AW, "German Dub", path_id) is None


def test_same_series_can_track_two_languages_and_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("H0MELAB_LANG_SEPARATION", "1")
    path_id = db.add_custom_path("Other", str(tmp_path / "other"))
    add_row()
    add_row(language="English Dub")
    add_row(path_id=path_id)
    assert len(db.get_autosync_series()) == 3


def test_tracked_series_can_be_paused_and_removed():
    row = add_row()
    assert db.update_autosync_series(row["id"], enabled=0)
    assert db.get_autosync_series_item(row["id"])["enabled"] == 0
    assert db.remove_autosync_series(row["id"])
    assert db.get_autosync_series() == []


@pytest.mark.parametrize(
    "given,expected,site",
    [
        (AW + "/", AW, "aniworld"),
        ("https://s.to/serie/stream/dark/", STO, "sto"),
        ("https://serienstream.cx/serie/dark", STO, "sto"),
    ],
)
def test_series_urls_are_canonical(given, expected, site):
    assert autosync.canonical_series_url(given) == (expected, site)


def test_season_or_episode_urls_are_rejected():
    with pytest.raises(ValueError, match="series page"):
        autosync.canonical_series_url(AW + "/staffel-1/episode-1")


def test_add_records_language_specific_baseline(monkeypatch):
    offered = inventory(2)
    monkeypatch.setattr(
        autosync, "_remote_inventory", lambda url, language: (FakeSeries(), offered)
    )
    monkeypatch.setattr(autosync, "_validated_provider", lambda name: "VOE")
    row = autosync.add_subscription(
        series_url=AW, language="German Dub", provider="VOE"
    )
    assert set(json.loads(row["baseline_episodes"])) == {"1:1", "1:2"}
    assert row["site"] == "aniworld"


def test_add_supports_serienstream(monkeypatch):
    monkeypatch.setattr(
        autosync,
        "_remote_inventory",
        lambda url, language: (FakeSeries("Dark"), inventory(1, site="sto")),
    )
    monkeypatch.setattr(autosync, "_validated_provider", lambda name: "VOE")
    row = autosync.add_subscription(
        series_url="https://serienstream.cx/serie/dark",
        language="German Dub",
        provider="VOE",
    )
    assert row["series_url"] == STO
    assert row["site"] == "sto"


def test_add_serienstream_persists_german_soft_subtitles(monkeypatch):
    monkeypatch.setattr(
        autosync,
        "_remote_inventory",
        lambda url, language: (FakeSeries("Dark"), inventory(1, site="sto")),
    )
    monkeypatch.setattr(autosync, "_validated_provider", lambda name: "VOE")
    row = autosync.add_subscription(
        series_url=STO,
        language="German Dub",
        provider="VOE",
        subtitle_language="deu",
    )
    assert row["subtitle_language"] == "deu"


def test_aniworld_language_flags_are_read_per_episode():
    series = AniworldSeries(AW)
    season = AniworldSeason(f"{AW}/staffel-1", series=series)
    season._AniworldSeason__html = """
      <tr itemtype="http://schema.org/Episode">
        <td><a href="/anime/stream/naruto/staffel-1/episode-1">Episode 1</a></td>
        <td><img src="/public/img/german.svg"><img src="/public/img/japanese-english.svg"></td>
      </tr>
      <tr itemtype="http://schema.org/Episode">
        <td><a href="/anime/stream/naruto/staffel-1/episode-2">Episode 2</a></td>
        <td><img src="/public/img/english.svg"></td>
      </tr>
    """
    assert season.episode_languages == {
        1: ("German Dub", "English Sub"),
        2: ("English Dub",),
    }


def test_add_rejects_languages_the_source_does_not_offer():
    with pytest.raises(ValueError, match="not supported"):
        autosync.add_subscription(series_url=STO, language="German Sub")


def test_add_rejects_a_deleted_custom_path():
    with pytest.raises(ValueError, match="no longer exists"):
        autosync.add_subscription(
            series_url=AW, language="German Dub", custom_path_id=999
        )


def test_add_rejects_two_languages_in_one_unseparated_destination(monkeypatch):
    add_row()
    monkeypatch.setattr(
        autosync,
        "_remote_inventory",
        lambda url, language: pytest.fail("conflict should be caught before fetching"),
    )
    with pytest.raises(RuntimeError, match="language-separated"):
        autosync.add_subscription(series_url=AW, language="English Dub")


def test_add_checks_provider_before_fetching(monkeypatch):
    monkeypatch.setattr(
        autosync,
        "_remote_inventory",
        lambda url, language: pytest.fail("invalid provider should fail first"),
    )
    with pytest.raises(ValueError, match="Unsupported hoster"):
        autosync.add_subscription(
            series_url=AW, language="German Dub", provider="DefinitelyMissing"
        )


def test_add_rejects_an_exact_duplicate_before_fetching(monkeypatch):
    add_row()
    monkeypatch.setattr(
        autosync,
        "_remote_inventory",
        lambda url, language: pytest.fail("duplicate should be caught before fetching"),
    )
    with pytest.raises(autosync.DuplicateSubscription):
        autosync.add_subscription(series_url=AW, language="German Dub")


def test_gap_mode_queues_every_missing_episode(monkeypatch, downloads):
    monkeypatch.setenv("H0MELAB_LANG_SEPARATION", "1")
    folder = downloads / "german-dub" / "Naruto"
    folder.mkdir(parents=True)
    (folder / "Naruto S01E001.mkv").write_bytes(b"x")
    row = add_row(baseline=["1:1", "1:2"])
    monkeypatch.setattr(
        autosync,
        "_remote_inventory",
        lambda url, language: (FakeSeries(), inventory(3)),
    )

    result = autosync._handle(row)
    assert result["status"] == "queued"
    queued = json.loads(db.get_queue_item(result["queue_id"])["episodes"])
    assert queued == [episode(2), episode(3)]


def test_serienstream_autosync_forwards_subtitles_to_queue(monkeypatch, downloads):
    row = add_row(
        url=STO,
        site="sto",
        title="Dark",
        subtitle_language="deu",
    )
    monkeypatch.setattr(
        autosync,
        "_remote_inventory",
        lambda url, language: (FakeSeries("Dark"), inventory(1, site="sto")),
    )
    monkeypatch.setattr(autosync, "_downloaded", lambda _subscription: set())

    result = autosync._handle(row)

    queued = db.get_queue_item(result["queue_id"])
    assert queued["source"] == "autosync"
    assert queued["subtitle_language"] == "deu"


def test_autosync_existing_episode_requires_requested_subtitle(tmp_path, monkeypatch):
    folder = tmp_path / "Dark" / "Season 01"
    folder.mkdir(parents=True)
    episode_file = folder / "Dark S01E001.mkv"
    episode_file.write_bytes(b"video")
    monkeypatch.setattr(autosync, "languages_from_probe", lambda _path: {"German Dub"})
    monkeypatch.setattr(autosync, "subtitle_languages_from_probe", lambda _path: set())

    assert autosync.episodes_in_folder(folder.parent, "German Dub", "deu") == set()

    monkeypatch.setattr(
        autosync, "subtitle_languages_from_probe", lambda _path: {"deu"}
    )
    assert autosync.episodes_in_folder(folder.parent, "German Dub", "deu") == {(1, 1)}


def test_new_only_ignores_baseline_but_queues_later_episodes(monkeypatch, downloads):
    monkeypatch.setenv("H0MELAB_AUTOSYNC_NEW_ONLY", "1")
    monkeypatch.setenv("H0MELAB_LANG_SEPARATION", "1")
    (downloads / "german-dub" / "Naruto").mkdir(parents=True)
    row = add_row(baseline=["1:1", "1:2"])
    monkeypatch.setattr(
        autosync,
        "_remote_inventory",
        lambda url, language: (FakeSeries(), inventory(3)),
    )

    result = autosync._handle(row)
    queued = json.loads(db.get_queue_item(result["queue_id"])["episodes"])
    assert queued == [episode(3)]


def test_new_only_retries_a_new_episode_until_it_exists(monkeypatch, downloads):
    monkeypatch.setenv("H0MELAB_AUTOSYNC_NEW_ONLY", "1")
    monkeypatch.setenv("H0MELAB_LANG_SEPARATION", "1")
    (downloads / "german-dub" / "Naruto").mkdir(parents=True)
    row = add_row(baseline=["1:1"])
    monkeypatch.setattr(
        autosync,
        "_remote_inventory",
        lambda url, language: (FakeSeries(), inventory(2)),
    )
    first = autosync._handle(row)
    db.set_queue_status(first["queue_id"], "failed")
    second = autosync._handle(row)
    assert second["status"] == "queued"


def test_new_only_fails_closed_if_the_saved_baseline_is_corrupt(monkeypatch, downloads):
    monkeypatch.setenv("H0MELAB_AUTOSYNC_NEW_ONLY", "1")
    monkeypatch.setenv("H0MELAB_LANG_SEPARATION", "1")
    (downloads / "german-dub" / "Naruto").mkdir(parents=True)
    row = add_row()
    with db.session() as conn:
        conn.execute(
            "UPDATE autosync_series SET baseline_episodes = ? WHERE id = ?",
            ("not-json", row["id"]),
        )
    row = db.get_autosync_series_item(row["id"])
    monkeypatch.setattr(
        autosync,
        "_remote_inventory",
        lambda url, language: (FakeSeries(), inventory(2)),
    )
    with pytest.raises(RuntimeError, match="baseline is invalid"):
        autosync._handle(row)


def test_an_active_queue_entry_prevents_a_duplicate(monkeypatch):
    row = add_row()
    monkeypatch.setattr(
        autosync,
        "_remote_inventory",
        lambda url, language: (FakeSeries(), inventory(1)),
    )
    db.add_to_queue(
        title="Naruto",
        series_url=AW,
        episodes=[episode(1)],
        language="German Dub",
        provider="VOE",
    )
    result = autosync._handle(row)
    assert result["status"] == "skipped"
    refreshed = db.get_autosync_series_item(row["id"])
    assert refreshed["last_checked_at"]
    assert refreshed["last_error"] is None


def test_associated_aniworld_movies_are_not_part_of_episode_autosync(monkeypatch):
    class FakeEpisode:
        episode_number = 1
        url = f"{AW}/filme/film-1"

    class FakeSeason:
        are_movies = True
        season_number = 0

        def __init__(self):
            self.episodes = [FakeEpisode()]

        @property
        def episode_languages(self):
            pytest.fail("movie language inventory should not be inspected")

    class FakeProvider:
        def series_cls(self, url):
            series = FakeSeries()
            series.seasons = [FakeSeason()]
            return series

    monkeypatch.setattr(autosync, "resolve_provider", lambda url: FakeProvider())
    _series, offered = autosync._remote_inventory(AW, "German Dub")
    assert offered == {}


def test_non_separated_library_checks_each_files_language(monkeypatch, downloads):
    folder = downloads / "Naruto"
    folder.mkdir()
    german = folder / "Naruto S01E001.mkv"
    english = folder / "Naruto S01E002.mkv"
    german.write_bytes(b"x")
    english.write_bytes(b"x")
    monkeypatch.setattr(
        autosync,
        "languages_from_probe",
        lambda path: {"German Dub"} if path == german else {"English Dub"},
    )
    assert autosync.episodes_in_folder(folder, "German Dub") == {(1, 1)}
    assert autosync.episodes_in_folder(folder, "English Dub") == {(1, 2)}


def test_unknown_file_language_does_not_redownload_decorated_silo_folder(
    monkeypatch, downloads
):
    folder = downloads / "Silo (2023) [imdbid-tt14688458]" / "Season 01"
    folder.mkdir(parents=True)
    for number in (1, 2):
        (folder / f"Silo S01E{number:03d}.mkv").write_bytes(b"video")

    monkeypatch.setattr(autosync, "languages_from_probe", lambda _path: set())
    assert autosync.episodes_in_folder(folder.parent, "German Dub") == {
        (1, 1),
        (1, 2),
    }

    row = add_row(title="Silo")
    monkeypatch.setattr(
        autosync,
        "_remote_inventory",
        lambda _url, _language: (FakeSeries("Silo"), inventory(2)),
    )
    result = autosync._handle(row)
    assert result["status"] == "up-to-date"
    assert db.get_queue() == []


def test_a_deleted_destination_fails_closed(monkeypatch, tmp_path):
    path_id = db.add_custom_path("Gone", str(tmp_path / "gone"))
    row = add_row(path_id=path_id)
    db.remove_custom_path(path_id)
    monkeypatch.setattr(
        autosync,
        "_remote_inventory",
        lambda url, language: (FakeSeries(), inventory(1)),
    )
    with pytest.raises(RuntimeError, match="no longer exists"):
        autosync._handle(row)


def test_cycle_checks_only_enabled_entries_and_isolates_errors(monkeypatch):
    good = add_row(title="Good")
    bad = add_row(url=AW.replace("naruto", "broken"), title="Broken")
    add_row(url=AW.replace("naruto", "paused"), title="Paused", enabled=False)

    def handle(row):
        if row["id"] == bad["id"]:
            raise RuntimeError("source unavailable")
        return {"id": row["id"], "status": "up-to-date"}

    monkeypatch.setattr(autosync, "_handle", handle)
    report = autosync.run_cycle()
    assert report["checked"] == 2
    assert {item["status"] for item in report["results"]} == {"up-to-date", "error"}
    assert db.get_autosync_series_item(bad["id"])["last_error"] == "source unavailable"
    assert good["id"] in {item["id"] for item in report["results"]}


def test_empty_cycle_never_scans_the_library(monkeypatch):
    monkeypatch.setattr(
        autosync, "_matching_folders", lambda *args: pytest.fail("library was scanned")
    )
    report = autosync.run_cycle()
    assert report["checked"] == 0


def test_status_exposes_list_configuration():
    add_row()
    report = autosync.status()
    assert report["tracked"] == 1
    assert report["languages"]["sto"] == ["German Dub", "English Dub"]
    assert "providers" in report


def test_series_api_lists_adds_pauses_and_removes(client, monkeypatch):
    monkeypatch.setenv("H0MELAB_ENABLE_AUTOSYNC", "1")
    monkeypatch.setattr(
        autosync,
        "add_subscription",
        lambda **kwargs: add_row(language=kwargs["language"]),
    )
    added = client.post(
        "/api/autosync/series",
        json={"series_url": AW, "language": "German Dub", "provider": "VOE"},
    )
    assert added.status_code == 201
    row_id = added.get_json()["series"]["id"]
    assert len(client.get("/api/autosync/series").get_json()["series"]) == 1
    assert (
        client.patch(
            f"/api/autosync/series/{row_id}", json={"enabled": False}
        ).get_json()["series"]["enabled"]
        == 0
    )
    assert client.delete(f"/api/autosync/series/{row_id}").status_code == 200


def test_series_state_api_matches_the_modal_selection(client, monkeypatch, tmp_path):
    monkeypatch.setenv("H0MELAB_ENABLE_AUTOSYNC", "1")
    path_id = db.add_custom_path("Series", str(tmp_path / "series"))
    row = add_row(
        url=STO,
        site="sto",
        title="Dark",
        language="English Dub",
        path_id=path_id,
        enabled=False,
    )

    response = client.get(
        "/api/autosync/series/state",
        query_string={
            "url": "https://s.to/serie/stream/dark/",
            "language": "English Dub",
            "custom_path_id": path_id,
        },
    )
    assert response.status_code == 200
    assert response.get_json()["tracked"] is True
    assert response.get_json()["series"]["id"] == row["id"]
    assert response.get_json()["series"]["enabled"] == 0

    unmatched = client.get(
        "/api/autosync/series/state",
        query_string={"url": STO, "language": "German Dub"},
    )
    assert unmatched.status_code == 200
    assert unmatched.get_json() == {"series": None, "tracked": False}


@pytest.mark.parametrize("path_id", ["nope", "0", "-1"])
def test_series_state_api_rejects_invalid_path_id(client, monkeypatch, path_id):
    monkeypatch.setenv("H0MELAB_ENABLE_AUTOSYNC", "1")
    response = client.get(
        "/api/autosync/series/state",
        query_string={
            "url": AW,
            "language": "German Dub",
            "custom_path_id": path_id,
        },
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "payload",
    [
        {"series_url": 123, "language": "German Dub", "provider": "VOE"},
        {"series_url": AW, "language": 123, "provider": "VOE"},
        {"series_url": AW, "language": "German Dub", "provider": 123},
        {
            "series_url": AW,
            "language": "German Dub",
            "provider": "VOE",
            "custom_path_id": "1",
        },
    ],
)
def test_series_api_rejects_wrong_json_types(client, monkeypatch, payload):
    monkeypatch.setenv("H0MELAB_ENABLE_AUTOSYNC", "1")
    response = client.post("/api/autosync/series", json=payload)
    assert response.status_code == 400


def test_series_api_rejects_a_string_boolean(client, monkeypatch):
    monkeypatch.setenv("H0MELAB_ENABLE_AUTOSYNC", "1")
    row = add_row()
    response = client.patch(
        f"/api/autosync/series/{row['id']}", json={"enabled": "false"}
    )
    assert response.status_code == 400
    assert db.get_autosync_series_item(row["id"])["enabled"] == 1


def test_series_api_rejects_a_non_string_provider_update(client, monkeypatch):
    monkeypatch.setenv("H0MELAB_ENABLE_AUTOSYNC", "1")
    row = add_row()
    response = client.patch(f"/api/autosync/series/{row['id']}", json={"provider": 123})
    assert response.status_code == 400


def test_series_api_updates_serienstream_subtitle_setting(client, monkeypatch):
    monkeypatch.setenv("H0MELAB_ENABLE_AUTOSYNC", "1")
    row = add_row(url=STO, site="sto", title="Dark")
    response = client.patch(
        f"/api/autosync/series/{row['id']}", json={"subtitle_language": "deu"}
    )
    assert response.status_code == 200
    assert response.get_json()["series"]["subtitle_language"] == "deu"


def test_series_api_cannot_resume_a_conflicting_language(client, monkeypatch):
    monkeypatch.setenv("H0MELAB_ENABLE_AUTOSYNC", "1")
    german = add_row()
    english = add_row(language="English Dub", enabled=False)
    assert german["enabled"] == 1
    response = client.patch(
        f"/api/autosync/series/{english['id']}", json={"enabled": True}
    )
    assert response.status_code == 409
    assert db.get_autosync_series_item(english["id"])["enabled"] == 0


def test_autosync_page_explains_the_explicit_list(client, monkeypatch):
    monkeypatch.setenv("H0MELAB_ENABLE_AUTOSYNC", "1")
    body = client.get("/autosync").get_data(as_text=True)
    assert "checks only the series" in body
    assert "SerienStream" in body
    assert "new episodes" in body
    assert 'id="seriesSite"' not in body
    assert 'id="seriesSearch"' not in body


def test_series_modal_uses_an_autosync_checkbox(client, monkeypatch):
    monkeypatch.setenv("H0MELAB_ENABLE_AUTOSYNC", "1")
    body = client.get("/").get_data(as_text=True)
    assert 'type="checkbox" id="autosyncToggle"' in body
    assert 'id="autosyncRow" hidden' not in body
    assert 'id="addAutosyncBtn"' not in body


def test_series_modal_hides_autosync_when_the_feature_is_disabled(client):
    body = client.get("/").get_data(as_text=True)
    assert 'id="autosyncToggle"' not in body
    assert 'id="subtitleSelect"' in body


def _ran(hours_ago):
    db.set_autosync_state(
        last_run=(datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    )


def test_fresh_interval_install_is_due_and_default_is_a_day():
    assert autosync._due() is True
    _ran(2)
    assert autosync._due() is False
    _ran(25)
    assert autosync._due() is True


def test_fixed_schedule_is_counted_from_enable_time(monkeypatch):
    monkeypatch.setenv("H0MELAB_AUTOSYNC_MODE", "cron")
    monkeypatch.setenv("H0MELAB_AUTOSYNC_CRON", "* * * * *")
    assert autosync._due() is False
    assert autosync.next_run_at() > autosync._now()


def test_future_clock_junk_is_ignored():
    db.set_autosync_state(
        last_run=(datetime.now(UTC) + timedelta(days=900)).isoformat()
    )
    assert autosync._due() is True
