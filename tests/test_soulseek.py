"""Music tab: Soulseek search, downloads and discovery via the aioslsk service."""

import pytest

from h0melab.web import soulseek

SEARCH_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def service(monkeypatch):
    """The service's HTTP API, answered from a dict."""
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


def test_settings_validate_and_keep_password_secret(client):
    assert client.put("/api/settings", json={"soulseek": {"listen_port": 80}}).status_code == 400
    assert client.put("/api/settings", json={"soulseek": {"username": "a\nb"}}).status_code == 400
    ok = client.put(
        "/api/settings",
        json={"soulseek": {"username": "me", "password": "pw-123", "listen_port": "50400", "share_downloads": False}},
    )
    assert ok.status_code == 200
    cfg = client.get("/api/settings").get_json()["soulseek"]
    assert cfg["username"] == "me" and cfg["password_set"] is True and "password" not in cfg
    assert cfg["listen_port"] == 50400 and cfg["share_downloads"] is False
    assert cfg["music_path_effective"].endswith("Music")


def test_status_reports_switched_off(client, monkeypatch):
    monkeypatch.setenv("H0MELAB_SOULSEEK_ENABLED", "0")
    assert client.get("/api/music/status").get_json() == {"configured": False}


def test_status_while_the_client_installs(client):
    from h0melab.web import soulseek_runtime

    soulseek_runtime._set("installing", "Downloading the newest Soulseek client (aioslsk)…")
    body = client.get("/api/music/status").get_json()
    assert body["configured"] and body["phase"] == "installing" and body["reachable"] is False
    soulseek_runtime._set("idle")


def test_search_flow(client, service):
    calls, answers = service
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


def test_search_rejects_bad_input(client, service):
    assert client.post("/api/music/search", json={"q": "a"}).status_code == 400
    assert client.get("/api/music/search/not-a-uuid").status_code == 400


def test_download_forwards_files_to_slskd(client, service):
    calls, _ = service
    response = client.post(
        "/api/music/download",
        json={"username": "a b", "files": [{"filename": "x\\y.flac", "size": 9}]},
    )
    assert response.get_json() == {"queued": 1}
    assert calls[-1] == ("POST", "/transfers/downloads/a%20b", [{"filename": "x\\y.flac", "size": 9}], None)
    bad = client.post("/api/music/download", json={"username": "a", "files": [{"filename": "x", "size": -1}]})
    assert bad.status_code == 400


def test_transfers_are_grouped_with_overall_state(client, service):
    _, answers = service
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


def test_cover_preview_is_fetched_cached_and_cleaned_up(client, service, music_dir):
    calls, answers = service
    body = {"username": "alice", "filename": "x\\Album\\cover.jpg", "size": 3}

    first = client.post("/api/music/cover", json=body).get_json()
    assert first["state"] == "pending"
    assert calls[-1][:3] == ("POST", "/transfers/downloads/alice", [{"filename": "x\\Album\\cover.jpg", "size": 3}])

    # service finished it: the file sits in <music>/Album/cover.jpg
    (music_dir / "Album").mkdir(parents=True)
    (music_dir / "Album" / "cover.jpg").write_bytes(b"jpg")
    answers[("GET", "/transfers/downloads/alice")] = {
        "directories": [{"files": [{"id": "t1", "filename": "x\\Album\\cover.jpg", "state": "Completed, Succeeded"}]}]
    }
    done = client.post("/api/music/cover", json=body).get_json()
    assert done == {"state": "ready", "key": first["key"]}
    # The preview left nothing behind, neither on disk nor in service's list
    assert not (music_dir / "Album").exists()
    assert ("DELETE", "/transfers/downloads/alice/t1", None, {"remove": "true"}) in calls

    image = client.get(f"/api/music/cover/{first['key']}")
    assert image.status_code == 200 and image.data == b"jpg" and image.mimetype == "image/jpeg"
    assert client.get("/api/music/cover/../../etc").status_code == 404


def test_album_download_keeps_its_cover_and_shows_it(client, service, music_dir):
    _, answers = service
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


def test_cover_requests_are_validated(client, service, music_dir):
    bad = {"username": "a", "filename": "x\\song.flac", "size": 3}
    assert client.post("/api/music/cover", json=bad).status_code == 400
    assert soulseek.local_file("..\\..\\evil.jpg") is None


def test_discover_rows_use_only_soulseek_data(client, service):
    _, answers = service
    answers[("GET", "/discover/recommendations")] = [
        {"item": "electronic", "score": 90},
        {"item": "  ", "score": 80},
        {"item": "daft punk", "score": 70},
    ]
    answers[("GET", "/discover/rooms")] = [{"name": "Techno", "userCount": 321}]
    rows = client.get("/api/music/discover?row=global").get_json()["items"]
    assert [r["title"] for r in rows] == ["electronic", "daft punk"]
    assert all(r["kind"] == "interest" and "cover_url" not in r for r in rows)
    [room] = client.get("/api/music/discover?row=rooms").get_json()["items"]
    assert room == {"kind": "room", "title": "Techno", "users": 321, "query": "Techno"}
    assert client.get("/api/music/discover?row=deezer").status_code == 400


def test_recent_row_shows_finished_albums_with_covers(client, service, music_dir):
    _, answers = service
    (music_dir / "Album").mkdir(parents=True)
    (music_dir / "Album" / "cover.jpg").write_bytes(b"jpg")
    answers[("GET", "/transfers/downloads")] = [
        {
            "username": "alice",
            "directories": [
                {
                    "directory": "x\\Album",
                    "files": [
                        {"id": "1", "filename": "x\\Album\\cover.jpg", "state": "Completed, Succeeded", "size": 3,
                         "localPath": str(music_dir / "Album" / "cover.jpg")},
                        {"id": "2", "filename": "x\\Album\\01.flac", "state": "Completed, Succeeded", "size": 9},
                    ],
                },
                {"directory": "x\\Busy", "files": [{"id": "3", "filename": "x\\Busy\\1.mp3", "state": "InProgress", "size": 9}]},
            ],
        }
    ]
    [item] = client.get("/api/music/discover?row=recent").get_json()["items"]
    assert item["title"] == "Album" and item["cover"] == soulseek.cover_key("alice", "x\\Album\\cover.jpg")


def test_liking_a_recommendation_is_sent_and_remembered(client, service, tmp_path, monkeypatch):
    from h0melab.web import soulseek_runtime

    monkeypatch.setattr(soulseek_runtime, "base_dir", lambda: tmp_path)
    calls, _ = service
    assert client.post("/api/music/like", json={"item": "  daft   punk "}).get_json() == {"item": "daft punk"}
    assert ("POST", "/interests", {"item": "daft punk"}, None) in calls
    assert (tmp_path / "likes.json").read_text() == '["daft punk"]'
    assert client.post("/api/music/like", json={"item": ""}).status_code == 400


def test_generated_account_is_stable(tmp_path, monkeypatch):
    from h0melab.web import soulseek_runtime

    monkeypatch.setattr(soulseek_runtime, "base_dir", lambda: tmp_path)
    first = soulseek_runtime.credentials()
    assert first[0].startswith("h0melab-") and first[2] is True
    assert soulseek_runtime.credentials() == first
    monkeypatch.setenv("H0MELAB_SOULSEEK_USERNAME", "mine")
    monkeypatch.setenv("H0MELAB_SOULSEEK_PASSWORD", "secret")
    assert soulseek_runtime.credentials() == ("mine", "secret", False)


def test_service_unavailable_while_starting(monkeypatch):
    from h0melab.web import soulseek_runtime

    soulseek_runtime._set("starting", "Starting the Soulseek client…")
    with pytest.raises(soulseek.SoulseekError) as caught:
        soulseek._connection()
    assert caught.value.status == 503
    soulseek_runtime._set("idle")


def test_service_script_stays_separate_from_the_app():
    """The GPL service must never import the downloader (and vice versa)."""
    from pathlib import Path

    from h0melab.web import soulseek_runtime

    script = soulseek_runtime.SERVICE_SCRIPT
    text = script.read_text(encoding="utf-8")
    assert "GPL-3.0-or-later" in text.splitlines()[0]
    assert "h0melab" not in "".join(
        line for line in text.splitlines() if line.startswith(("import ", "from "))
    )
    assert (script.parent / "LICENSE").is_file()
    app_sources = Path(soulseek.__file__).parent.rglob("*.py")
    assert not any("aioslsk" in line and line.lstrip().startswith(("import", "from"))
                   for source in app_sources for line in source.read_text(encoding="utf-8").splitlines())


def test_queue_page_has_the_soulseek_section(client):
    html = client.get("/queue").get_data(as_text=True)
    assert 'id="soulseekQueue"' in html and "soulseek-queue.js" in html
    music = client.get("/music").get_data(as_text=True)
    assert 'data-row="global"' in music and "musicTransfers" not in music and "deezer" not in music.lower()


def _finished(music_dir, *names, state="Completed, Succeeded"):
    album = music_dir / "Album"
    album.mkdir(parents=True, exist_ok=True)
    files = []
    for index, name in enumerate(names):
        (album / name).write_bytes(b"audio")
        files.append({"id": str(index), "filename": f"x/Album/{name}", "state": state,
                      "size": 5, "localPath": str(album / name)})
    return [{"username": "alice", "directories": [{"directory": "x/Album", "files": files}]}]


@pytest.fixture
def converter(monkeypatch, service, music_dir):
    from h0melab.web import music_convert

    music_convert._failed.clear()
    runs = []

    def fake_run(command):
        runs.append(command)
        from pathlib import Path
        from types import SimpleNamespace

        Path(command[-1]).write_bytes(b"mp3")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(music_convert, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(music_convert, "_run", fake_run)
    monkeypatch.setenv(music_convert.KEYS["organize"], "0")
    music_convert._albums.clear()
    return music_convert, runs


def test_finished_downloads_are_converted_to_mp3(converter, service, music_dir, tmp_path):
    music_convert, runs = converter
    _, answers = service
    outside = tmp_path / "library" / "keep.flac"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    listing = _finished(music_dir, "01.flac", "02.mp3", "03.m4a")
    listing[0]["directories"][0]["files"].append(
        {"id": "9", "filename": "x/keep.flac", "state": "Completed, Succeeded", "localPath": str(outside)})
    answers[("GET", "/transfers/downloads")] = listing
    assert music_convert.convert_finished() == 2
    album = music_dir / "Album"
    assert sorted(p.name for p in album.iterdir()) == ["01.mp3", "02.mp3", "03.mp3"]
    assert outside.exists()  # never touches files outside the music folder
    assert "libmp3lame" in runs[0] and runs[0][runs[0].index("-b:a") + 1] == "320k"
    assert "attached_pic" in runs[0]


def test_unfinished_or_disabled_downloads_stay_untouched(converter, service, music_dir, monkeypatch):
    music_convert, runs = converter
    _, answers = service
    answers[("GET", "/transfers/downloads")] = _finished(music_dir, "01.flac", state="InProgress")
    assert music_convert.convert_finished() == 0
    answers[("GET", "/transfers/downloads")] = _finished(music_dir, "01.flac")
    monkeypatch.setenv("H0MELAB_MUSIC_CONVERT_MP3", "0")
    assert music_convert.convert_finished() == 0
    assert runs == [] and (music_dir / "Album" / "01.flac").exists()


def test_failed_conversion_keeps_the_original(converter, service, music_dir, monkeypatch):
    music_convert, runs = converter
    _, answers = service
    from types import SimpleNamespace

    def broken(command):
        runs.append(command)
        return SimpleNamespace(returncode=1, stderr="bad file")

    monkeypatch.setattr(music_convert, "_run", broken)
    answers[("GET", "/transfers/downloads")] = _finished(music_dir, "01.flac")
    assert music_convert.convert_finished() == 0
    # Retried once without the cover stream, then given up for good
    assert len(runs) == 2 and "attached_pic" not in runs[1]
    assert (music_dir / "Album" / "01.flac").exists()
    assert not list((music_dir / "Album").glob("*.mp3"))
    assert music_convert.convert_finished() == 0 and len(runs) == 2


def test_mp3_settings_round_trip(client):
    assert client.put("/api/settings", json={"soulseek": {"mp3_bitrate": "999k"}}).status_code == 400
    assert client.put("/api/settings", json={"soulseek": {"convert_mp3": False, "mp3_bitrate": "V0"}}).status_code == 200
    cfg = client.get("/api/settings").get_json()["soulseek"]
    assert cfg["convert_mp3"] is False and cfg["mp3_bitrate"] == "V0"


def test_non_music_interests_are_left_out(client, service):
    _, answers = service
    answers[("GET", "/discover/recommendations")] = [
        {"item": name, "score": 1} for name in ("linux", "Piracy", "flac", ":3", "jazz", "drum & bass")
    ]
    rows = client.get("/api/music/discover?row=global").get_json()["items"]
    assert [r["title"] for r in rows] == ["jazz", "drum & bass"]


def test_nameless_downloads_get_their_real_name_back(converter, service, music_dir):
    music_convert, _ = converter
    _, answers = service
    album = music_dir / "Stadtaffe"
    album.mkdir(parents=True)
    files = []
    for index, title in enumerate(["01 - Alles neu.flac", "02 - Schwarz zu blau.mp3"], start=1):
        local = album / f" ({index})"
        local.write_bytes(b"audio")
        files.append({"id": str(index), "filename": rf"@@music\Peter Fox\Stadtaffe\{title}",
                      "state": "Completed, Succeeded", "localPath": str(local)})
    answers[("GET", "/transfers/downloads")] = [
        {"username": "alice", "directories": [{"directory": "x", "files": files}]}]
    assert music_convert.convert_finished() == 1
    assert sorted(p.name for p in album.iterdir()) == ["01 - Alles neu.mp3", "02 - Schwarz zu blau.mp3"]


def test_service_names_files_after_the_remote_file():
    import ast
    from pathlib import Path

    import h0melab

    # The service runs in its own venv with aioslsk, so read it instead of importing
    source = (Path(h0melab.__file__).parent / "soulseek_service" / "service.py").read_text(encoding="utf-8")
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Assign) and any(
                 isinstance(t, ast.Attribute) and t.attr == "naming_strategies" for t in node.targets)]
    names = [element.func.id for element in calls[0].value.elts]
    assert names[0] == "DefaultNamingStrategy"


@pytest.fixture
def library(converter, monkeypatch):
    """Converter with the Jellyfin layout on, fake tags and a recorded refresh."""
    from h0melab.web import jellyfin

    music_convert, _ = converter
    monkeypatch.setenv(music_convert.KEYS["organize"], "1")
    tags = {}
    refreshed = []
    monkeypatch.setattr(music_convert, "_probe", lambda path: tags.get(path.stem, {}))
    monkeypatch.setattr(jellyfin, "refresh", lambda kind: refreshed.append(kind) or True)
    return music_convert, tags, refreshed


def _listing(music_dir, folder, names, remote="@@music/Peter Fox/Stadtaffe"):
    album = music_dir / folder
    album.mkdir(parents=True, exist_ok=True)
    files = []
    for index, (local_name, remote_name) in enumerate(names):
        (album / local_name).write_bytes(b"data")
        files.append({"id": str(index), "filename": f"{remote}/{remote_name}",
                      "state": "Completed, Succeeded", "localPath": str(album / local_name)})
    return [{"username": "alice", "directories": [{"directory": remote, "files": files}]}]


def test_albums_are_sorted_into_artist_and_album_folders(library, service, music_dir):
    music_convert, tags, refreshed = library
    _, answers = service
    tags["01 - Alles neu"] = {"album_artist": "Peter Fox", "artist": "Peter Fox feat. X",
                              "album": "Stadtaffe: Deluxe?"}
    answers[("GET", "/transfers/downloads")] = _listing(music_dir, "Stadtaffe", [
        (" (1)", "01 - Alles neu.flac"), ("02 - Haus am See.mp3", "02 - Haus am See.mp3"),
        ("front.jpg", "front.jpg")])
    assert music_convert.convert_finished() == 1
    album = music_dir / "Peter Fox" / "Stadtaffe_ Deluxe_"
    assert sorted(p.name for p in album.iterdir()) == [
        "01 - Alles neu.mp3", "02 - Haus am See.mp3", "cover.jpg"]
    assert not (music_dir / "Stadtaffe").exists()  # the client's folder is tidied up
    assert refreshed == ["music"]
    # A second pass finds nothing left to do and does not scan again
    assert music_convert.convert_finished() == 0 and refreshed == ["music"]


def test_untagged_albums_use_the_remote_folders(library, service, music_dir):
    music_convert = library[0]
    _, answers = service
    answers[("GET", "/transfers/downloads")] = _listing(
        music_dir, "Stadtaffe", [("03.mp3", "03.mp3")], remote="D:/Musik/Peter Fox/Stadtaffe")
    music_convert.convert_finished()
    assert (music_dir / "Peter Fox" / "Stadtaffe" / "03.mp3").is_file()


def test_existing_files_are_not_overwritten(library, service, music_dir):
    music_convert = library[0]
    target = music_dir / "Peter Fox" / "Stadtaffe"
    target.mkdir(parents=True)
    (target / "03.mp3").write_bytes(b"old")
    _, answers = service
    answers[("GET", "/transfers/downloads")] = _listing(music_dir, "Stadtaffe", [("03.mp3", "03.mp3")])
    music_convert.convert_finished()
    assert (target / "03.mp3").read_bytes() == b"old"
    assert (target / "03 (1).mp3").is_file()


def test_jellyfin_layout_setting_round_trip(monkeypatch):
    from h0melab.web import music_convert

    updates = {}
    music_convert.collect_settings({"jellyfin_layout": False}, updates)
    assert updates == {music_convert.KEYS["organize"]: "0"}
    monkeypatch.setenv(music_convert.KEYS["organize"], "0")
    assert music_convert.settings()["jellyfin_layout"] is False
