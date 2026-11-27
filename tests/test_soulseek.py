"""Music tab: slskd bridge for Soulseek search and downloads."""

import pytest

from h0melab.web import soulseek

SEARCH_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def slskd(monkeypatch):
    monkeypatch.setenv(soulseek.KEYS["url"], "http://slskd:5030")
    monkeypatch.setenv(soulseek.KEYS["api_key"], "k" * 20)
    calls = []
    answers = {}

    def fake(method, path, *, json=None, params=None, connection=None):
        calls.append((method, path, json, params))
        return answers.get((method, path))

    monkeypatch.setattr(soulseek, "_request", fake)
    return calls, answers


def _response(username, files, free=True, speed=1000):
    return {
        "username": username,
        "hasFreeUploadSlot": free,
        "uploadSpeed": speed,
        "queueLength": 0,
        "files": files,
    }


def test_responses_are_grouped_into_folders_and_non_audio_dropped():
    releases = soulseek.group_responses(
        [
            _response(
                "alice",
                [
                    {"filename": "@@music\\Artist\\Album\\02 B.flac", "size": 20, "bitDepth": 16, "sampleRate": 44100, "length": 100},
                    {"filename": "@@music\\Artist\\Album\\01 A.flac", "size": 10, "length": 50},
                    {"filename": "@@music\\Artist\\Album\\cover.jpg", "size": 5},
                ],
            ),
            _response("bob", [{"filename": "x/Single/song.mp3", "size": 3, "bitRate": 320}], free=False),
        ]
    )
    assert [r["username"] for r in releases] == ["alice", "bob"]
    album = releases[0]
    assert album["title"] == "Album" and album["parent"] == "Artist"
    assert [f["name"] for f in album["files"]] == ["01 A.flac", "02 B.flac"]
    assert album["size"] == 30 and album["length"] == 150
    assert album["lossless"] and album["formats"] == ["FLAC"]
    assert releases[1]["bitrate"] == 320 and not releases[1]["lossless"]


def test_settings_validate_url_and_keep_key_secret(client):
    bad = client.put("/api/settings", json={"soulseek": {"url": "ftp://x"}})
    assert bad.status_code == 400
    ok = client.put(
        "/api/settings",
        json={"soulseek": {"url": "http://slskd:5030/", "api_key": "secret-key-123456"}},
    )
    assert ok.status_code == 200
    cfg = client.get("/api/settings").get_json()["soulseek"]
    assert cfg["url"] == "http://slskd:5030" and cfg["key_set"] is True
    assert "api_key" not in cfg and cfg["music_path_effective"].endswith("Music")


def test_status_reports_unconfigured(client):
    assert client.get("/api/music/status").get_json() == {"configured": False}


def test_search_flow(client, slskd):
    calls, answers = slskd
    started = client.post("/api/music/search", json={"q": "  some   album "})
    search_id = started.get_json()["id"]
    assert calls[0][0] == "POST" and calls[0][2]["searchText"] == "some album"
    assert calls[0][2]["id"] == search_id

    answers[("GET", f"/searches/{SEARCH_ID}")] = {"isComplete": True, "responseCount": 1}
    answers[("GET", f"/searches/{SEARCH_ID}/responses")] = [
        _response("alice", [{"filename": "A\\B\\t.mp3", "size": 1}])
    ]
    state = client.get(f"/api/music/search/{SEARCH_ID}").get_json()
    assert state["complete"] and state["releases"][0]["title"] == "B"


def test_search_rejects_bad_input(client, slskd):
    assert client.post("/api/music/search", json={"q": "a"}).status_code == 400
    assert client.get("/api/music/search/not-a-uuid").status_code == 400


def test_download_forwards_files_to_slskd(client, slskd):
    calls, _ = slskd
    response = client.post(
        "/api/music/download",
        json={"username": "a b", "files": [{"filename": "x\\y.flac", "size": 9}]},
    )
    assert response.get_json() == {"queued": 1}
    assert calls[-1] == ("POST", "/transfers/downloads/a%20b", [{"filename": "x\\y.flac", "size": 9}], None)
    bad = client.post("/api/music/download", json={"username": "a", "files": [{"filename": "x", "size": -1}]})
    assert bad.status_code == 400


def test_transfers_are_grouped_with_overall_state(client, slskd):
    _, answers = slskd
    answers[("GET", "/transfers/downloads")] = [
        {
            "username": "alice",
            "directories": [
                {
                    "directory": "Music\\Album",
                    "files": [
                        {"id": "1", "filename": "Music\\Album\\1.flac", "state": "Completed, Succeeded", "size": 100, "bytesTransferred": 100},
                        {"id": "2", "filename": "Music\\Album\\2.flac", "state": "InProgress", "size": 100, "bytesTransferred": 50, "averageSpeed": 10},
                    ],
                }
            ],
        }
    ]
    [group] = client.get("/api/music/transfers").get_json()["transfers"]
    assert group["title"] == "Album" and group["state"] == "running"
    assert group["percent"] == 75.0 and group["completed"] == 1


def test_music_page_and_nav_link(client):
    page = client.get("/music")
    assert page.status_code == 200
    assert b"Soulseek" in page.data and b"/music" in client.get("/").data


def test_best_cover_is_picked_per_folder():
    [release] = soulseek.group_responses(
        [
            _response(
                "alice",
                [
                    {"filename": "A\\Album\\scan_back.jpg", "size": 900},
                    {"filename": "A\\Album\\Folder.JPG", "size": 100},
                    {"filename": "A\\Album\\huge.png", "size": soulseek.MAX_COVER_BYTES + 1},
                    {"filename": "A\\Album\\01.flac", "size": 10},
                ],
            )
        ]
    )
    assert release["cover"]["filename"] == "A\\Album\\Folder.JPG"
    assert release["cover"]["cached"] is False and release["file_count"] == 1


@pytest.fixture
def music_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(soulseek.KEYS["music_path"], str(tmp_path / "music"))
    monkeypatch.setattr(soulseek, "_cache_dir", lambda: tmp_path / "cache")
    soulseek._previews.clear()
    return tmp_path / "music"


def test_cover_preview_is_fetched_cached_and_cleaned_up(client, slskd, music_dir):
    calls, answers = slskd
    body = {"username": "alice", "filename": "x\\Album\\cover.jpg", "size": 3}

    first = client.post("/api/music/cover", json=body).get_json()
    assert first["state"] == "pending"
    assert calls[-1][:3] == ("POST", "/transfers/downloads/alice", [{"filename": "x\\Album\\cover.jpg", "size": 3}])

    # slskd finished it: the file sits in <music>/Album/cover.jpg
    (music_dir / "Album").mkdir(parents=True)
    (music_dir / "Album" / "cover.jpg").write_bytes(b"jpg")
    answers[("GET", "/transfers/downloads/alice")] = {
        "directories": [{"files": [{"id": "t1", "filename": "x\\Album\\cover.jpg", "state": "Completed, Succeeded"}]}]
    }
    done = client.post("/api/music/cover", json=body).get_json()
    assert done == {"state": "ready", "key": first["key"]}
    # The preview left nothing behind, neither on disk nor in slskd's list
    assert not (music_dir / "Album").exists()
    assert ("DELETE", "/transfers/downloads/alice/t1", None, {"remove": "true"}) in calls

    image = client.get(f"/api/music/cover/{first['key']}")
    assert image.status_code == 200 and image.data == b"jpg" and image.mimetype == "image/jpeg"
    assert client.get("/api/music/cover/../../etc").status_code == 404


def test_album_download_keeps_its_cover_and_shows_it(client, slskd, music_dir):
    _, answers = slskd
    soulseek._previews.add(("alice", "x\\Album\\cover.jpg"))
    client.post(
        "/api/music/download",
        json={"username": "alice", "files": [{"filename": "x\\Album\\cover.jpg", "size": 3}]},
    )
    assert not soulseek._previews

    (music_dir / "Album").mkdir(parents=True)
    (music_dir / "Album" / "cover.jpg").write_bytes(b"jpg")
    answers[("GET", "/transfers/downloads")] = [
        {
            "username": "alice",
            "directories": [
                {
                    "directory": "x\\Album",
                    "files": [
                        {"id": "1", "filename": "x\\Album\\cover.jpg", "state": "Completed, Succeeded", "size": 3},
                        {"id": "2", "filename": "x\\Album\\01.flac", "state": "Completed, Succeeded", "size": 9},
                    ],
                }
            ],
        }
    ]
    [group] = client.get("/api/music/transfers").get_json()["transfers"]
    assert group["cover"] == soulseek.cover_key("alice", "x\\Album\\cover.jpg")
    assert [f["name"] for f in group["files"]] == ["01.flac"]
    assert (music_dir / "Album" / "cover.jpg").exists()


def test_cover_requests_are_validated(client, slskd, music_dir):
    bad = {"username": "a", "filename": "x\\song.flac", "size": 3}
    assert client.post("/api/music/cover", json=bad).status_code == 400
    assert soulseek.local_file("..\\..\\evil.jpg") is None


def test_discover_rows_come_from_deezer_with_proxied_covers(client, monkeypatch):
    from h0melab.web import music_discover

    music_discover._cache.clear()
    seen = []

    def fake_get_json(url, *, allowed_hosts=()):
        seen.append((url, allowed_hosts))
        if "artists" in url:
            return {"data": [{"name": "Daft Punk", "picture_xl": "https://cdn.example/a.jpg"}]}
        return {"data": [{"title": "Discovery", "release_date": "2001-03-12",
                          "artist": {"name": "Daft Punk"}, "cover_xl": "https://cdn.example/c.jpg"}]}

    monkeypatch.setattr(music_discover, "get_json", fake_get_json)
    [album] = client.get("/api/music/discover?row=new_releases").get_json()["items"]
    assert album["query"] == "Daft Punk Discovery" and album["year"] == "2001"
    assert album["cover_url"].startswith("/api/catalog/cover?token=")
    [artist] = client.get("/api/music/discover?row=top_artists").get_json()["items"]
    assert artist["kind"] == "artist" and artist["query"] == "Daft Punk"
    assert all(hosts == ("api.deezer.com",) for _, hosts in seen)
    # Cached: a second visit does not ask Deezer again
    client.get("/api/music/discover?row=new_releases")
    assert len(seen) == 2
    assert client.get("/api/music/discover?row=nope").status_code == 400


def test_discover_failure_is_reported(client, monkeypatch):
    from h0melab.catalog.common import CatalogError
    from h0melab.web import music_discover

    music_discover._cache.clear()

    def broken(url, *, allowed_hosts=()):
        raise CatalogError("down")

    monkeypatch.setattr(music_discover, "get_json", broken)
    assert client.get("/api/music/discover?row=top_albums").status_code == 502


def test_queue_page_has_the_soulseek_section(client):
    html = client.get("/queue").get_data(as_text=True)
    assert 'id="soulseekQueue"' in html and "soulseek-queue.js" in html
    music = client.get("/music").get_data(as_text=True)
    assert 'data-row="new_releases"' in music and "musicTransfers" not in music
