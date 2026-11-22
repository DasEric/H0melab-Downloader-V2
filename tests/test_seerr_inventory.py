import pytest
from h0melab.web import seerr_files, seerr_inventory


@pytest.fixture
def identity():
    return {
        "media_type": "movie",
        "tmdb_id": 42,
        "imdb_id": "tt1234567",
        "title": "Example",
    }


def test_nfo_movie_is_imported_without_filename_identity(tmp_path, identity):
    video = tmp_path / "unrelated-name.mkv"
    video.write_bytes(b"media")
    video.with_suffix(".nfo").write_text(
        '<movie><uniqueid type="tmdb">42</uniqueid><imdbid>tt1234567</imdbid></movie>'
    )
    assert seerr_inventory.discover(tmp_path, identity) == 1
    assert (
        seerr_files.existing({"_seerr_identity": identity, "season": 0, "episode": 0})
        == video
    )
    assert video.read_bytes() == b"media"


@pytest.mark.parametrize(
    "xml",
    [
        "<movie><title>Example</title></movie>",
        "<movie><tmdbid>43</tmdbid></movie>",
        "<movie><tmdbid>42</tmdbid><imdbid>tt9999999</imdbid></movie>",
        "<tvshow><tmdbid>42</tmdbid></tvshow>",
        "<movie><tmdbid>42</tmdbid><tmdbid>43</tmdbid></movie>",
        '<!DOCTYPE movie [<!ENTITY x "42">]><movie><tmdbid>&x;</tmdbid></movie>',
    ],
)
def test_unverified_nfo_never_creates_receipt(tmp_path, identity, xml):
    (tmp_path / "Example.mkv").write_bytes(b"media")
    (tmp_path / "Example.nfo").write_text(xml)
    assert seerr_inventory.discover(tmp_path, identity) == 0


def test_episode_requires_series_id_and_explicit_numbers(tmp_path, identity):
    identity["media_type"] = "tv"
    show = tmp_path / "Different name"
    show.mkdir()
    (show / "tvshow.nfo").write_text("<tvshow><tmdbid>42</tmdbid></tvshow>")
    season = show / "Season 01"
    season.mkdir()
    video = season / "arbitrary.mkv"
    video.write_bytes(b"media")
    video.with_suffix(".nfo").write_text(
        "<episodedetails><season>1</season><episode>2</episode></episodedetails>"
    )
    assert seerr_inventory.discover(tmp_path, identity, [2]) == 0
    assert seerr_inventory.discover(tmp_path, identity, [1]) == 1
    assert (
        seerr_files.existing({"_seerr_identity": identity, "season": 1, "episode": 2})
        == video
    )


def test_movie_nfo_does_not_guess_between_multiple_files(tmp_path, identity):
    for name in ("one", "two"):
        (tmp_path / (name + ".mkv")).write_bytes(b"media")
    (tmp_path / "movie.nfo").write_text("<movie><tmdbid>42</tmdbid></movie>")
    assert seerr_inventory.discover(tmp_path, identity) == 0


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_nfo_dtd_rejected_independently_of_encoding(tmp_path, identity, encoding):
    (tmp_path / "film.mkv").write_bytes(b"media")
    xml = '<!DOCTYPE movie [<!ENTITY id "42">]><movie><tmdbid>&id;</tmdbid></movie>'
    (tmp_path / "film.nfo").write_bytes(xml.encode(encoding))
    assert seerr_inventory.discover(tmp_path, identity) == 0


def test_missing_receipt_file_is_not_existing(tmp_path, identity):
    path = tmp_path / "film.mkv"
    path.write_bytes(b"media")
    entry = {"_seerr_identity": identity, "season": 0, "episode": 0}
    seerr_files.record(entry, path)
    path.unlink()
    assert seerr_files.existing(entry) is None


def test_existing_series_folder_is_reused_for_missing_episodes(tmp_path, identity):
    identity["media_type"] = "tv"
    show = tmp_path / "Existing localized title"
    show.mkdir()
    (show / "tvshow.nfo").write_text("<tvshow><tmdbid>42</tmdbid></tvshow>")
    assert seerr_inventory.discover(tmp_path, identity, [1]) == 0
    assert seerr_files.folder(identity) == "Existing localized title"


def test_multiple_identified_show_folders_require_review(tmp_path, identity):
    identity["media_type"] = "tv"
    for name in ("One", "Two"):
        show = tmp_path / name
        show.mkdir()
        (show / "tvshow.nfo").write_text("<tvshow><tmdbid>42</tmdbid></tvshow>")
    with pytest.raises(ValueError, match="consolidation"):
        seerr_inventory.discover(tmp_path, identity, [1])
