"""Opt-in integration with an actual Jellyfin server and this Flask app.

Set CONNECTOR_JELLYFIN_TEST_URL to an isolated Jellyfin 12 instance containing
the built plugin. Synthetic providers replace external sites, not the queue,
file receipts, authentication, library scanning or deletion implementations.
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

import pytest
from werkzeug.serving import make_server

from h0melab.config import Audio, Subtitles
from h0melab.web import connector_files, db, worker
from h0melab.web.views import api_media


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Only this opt-in test may connect, and only to loopback fixtures."""
    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in (
            "127.0.0.1",
            "::1",
            "localhost",
        ):
            raise RuntimeError("Integration fixture only permits loopback connections")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.mark.parametrize("site", ["megakino", "filmpalast"])
def test_real_jellyfin_language_routing_and_owner_deletion(
    app, api_key, downloads, tmp_path, monkeypatch, site
):
    server_url = os.environ.get("CONNECTOR_JELLYFIN_TEST_URL")
    if not server_url:
        pytest.skip(
            "Set CONNECTOR_JELLYFIN_TEST_URL for an isolated real Jellyfin 12 test"
        )
    assert urlparse(server_url).hostname in ("localhost", "127.0.0.1"), (
        "Never use a production Jellyfin instance"
    )
    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is required for real media fixtures")
    password = "Connector-Integration-Only-2026!"
    header = 'MediaBrowser Client="Connector Integration", Device="Loopback Test", DeviceId="connector-integration", Version="1"'

    def jf(path, method="GET", body=None, token=None, expected=200, _startup_retries=0):
        headers = {"Authorization": header, "Content-Type": "application/json"}
        if token:
            headers["X-Emby-Token"] = token
            headers["Authorization"] += ', Token="' + token + '"'
        request = Request(
            server_url.rstrip("/") + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        try:
            with urlopen(request, timeout=90) as response:
                code, data = response.status, response.read()
        except HTTPError as error:
            code, data = error.code, error.read()
        if code == 503 and path == "/System/Info/Public" and _startup_retries < 120:
            time.sleep(0.5)
            return jf(path, method, body, token, expected, _startup_retries + 1)
        assert code == expected, (
            f"{method} {path}: HTTP {code}: {data.decode(errors='replace')[:1000]}"
        )
        return json.loads(data) if data else None

    deadline = time.monotonic() + 60
    info = jf("/System/Info/Public")
    while "Version" not in info and time.monotonic() < deadline:
        time.sleep(0.5)
        info = jf("/System/Info/Public")
    assert info["Version"].startswith("12.")
    if not info["StartupWizardCompleted"]:
        jf("/Startup/User")
        jf(
            "/Startup/User",
            "POST",
            {"Name": "connector-test-admin", "Password": password},
            expected=204,
        )
        jf("/Startup/Complete", "POST", expected=204)
    admin = jf(
        "/Users/AuthenticateByName",
        "POST",
        {"Username": "connector-test-admin", "Pw": password},
    )["AccessToken"]
    suffix = uuid.uuid4().hex[:8]
    users = []
    for name in ("alice-" + suffix, "bob-" + suffix):
        user = jf("/Users/New", "POST", {"Name": name, "Password": password}, admin)
        login = jf(
            "/Users/AuthenticateByName", "POST", {"Username": name, "Pw": password}
        )
        assert not login["User"]["Policy"]["IsAdministrator"]
        users.append((user["Id"], login["AccessToken"]))
    alice, bob = users[0][1], users[1][1]
    show = "Connector Integration Series " + suffix
    film = "Connector Integration Film " + suffix
    series_url = "https://megakino.test/serials/" + suffix
    film_url = "https://megakino.test/movies/" + suffix
    if site == "filmpalast":
        series_url = "https://filmpalast.to/stream/" + suffix + "-s01e01"
        film_url = "https://filmpalast.to/stream/film-" + suffix
    movies = tmp_path / "movies"
    movies.mkdir()
    movie_path = db.add_custom_path("Movies", str(movies), site)
    series_path = db.add_custom_path("Series", str(downloads))
    source_clips = {}
    for language, code in (("English Dub", "eng"), ("German Dub", "deu")):
        clip = tmp_path / (code + ".mkv")
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=size=16x16:duration=0.2",
                "-f",
                "lavfi",
                "-i",
                "anullsrc",
                "-shortest",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
                "-metadata:s:a:0",
                "language=" + code,
                str(clip),
            ],
            check=True,
            timeout=30,
        )
        source_clips[language] = clip

    class Series:
        def __init__(self, url, **kwargs):
            self.url = url
            self.is_series = "/serials/" in url or (
                site == "filmpalast" and suffix + "-s01e" in url
            )
            self.is_movie = not self.is_series
            self.title = show if self.is_series else film
            self.description = "Synthetic loopback integration fixture"
            self.release_year = "2026"
            self.poster_url = ""

    class Episode(Series):
        def __init__(
            self, url, selected_language="German Dub", selected_path=None, **kwargs
        ):
            super().__init__(url)
            self.selected_language = selected_language
            self.selected_path = selected_path
            fragment = urlparse(url).fragment
            self.number = (
                int(fragment.split("=", 1)[1]) if fragment.startswith("mkep=") else 1
            )
            if site == "filmpalast" and self.is_series:
                self.number = int(url.rsplit("s01e", 1)[1])
                self.title = show + " (2026) S01E" + str(self.number).zfill(2)
            self.series_episodes = [
                {"number": n, "label": "S01E" + str(n).zfill(2), "providers": ["VOE"]}
                for n in (1, 2)
            ]
            self.title_cleaned = self.title

        @property
        def provider_data(self):
            result = {(Audio.ENGLISH, Subtitles.NONE): ["VOE"]}
            if self.number == 1 or self.is_movie:
                result[(Audio.GERMAN, Subtitles.NONE)] = ["VOE"]
            return result

        @property
        def _episode_path(self):
            if self.is_series:
                path = (
                    Path(self.selected_path)
                    / show
                    / "Season 01"
                    / f"{show} S01E{self.number:03d}.mkv"
                )
            else:
                path = Path(self.selected_path) / film / (film + ".mkv")
            return connector_files.episode_path(self, path)

        def download(self):
            self._episode_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_clips[self.selected_language], self._episode_path)

    provider = SimpleNamespace(
        name="MegaKino" if site == "megakino" else "FilmPalast",
        series_cls=Series,
        episode_cls=Episode,
    )
    monkeypatch.setattr(api_media, "resolve_provider", lambda url: provider)
    monkeypatch.setattr(worker, "resolve_provider", lambda url: provider)
    monkeypatch.setattr(
        api_media.sitesearch,
        "search",
        lambda site, keyword: [
            {"title": show, "url": series_url, "poster": "", "is_series": True},
            {"title": film, "url": film_url, "poster": "", "is_series": False},
        ],
    )
    monkeypatch.setenv("H0MELAB_ENABLE_MEGAKINO", "1")
    if site == "filmpalast":
        from h0melab import search

        hits = [
            {
                "title": show + " (2026) S01E" + str(n).zfill(2),
                "url": series_url[:-2] + str(n).zfill(2),
                "poster": "",
            }
            for n in (1, 2)
        ]
        monkeypatch.setattr(
            api_media.sitesearch,
            "search",
            lambda *args: (
                hits
                + [
                    {
                        "title": film,
                        "url": film_url,
                        "poster": "",
                        "media_type": "movie",
                    }
                ]
            ),
        )
        monkeypatch.setattr(
            search,
            "query_filmpalast",
            lambda *args, **kwargs: search.CompleteSearchResults(hits, complete=True),
        )
    # The application setting reads ENABLE_MEGAKINO through settings_store.
    from h0melab.web import settings_store

    monkeypatch.setattr(settings_store, "enabled_sites", lambda: {site: True})
    upstream = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    raw_key, _ = api_key("admin")
    plugin_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    added_libraries = []

    def wait_for(predicate, label, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.3)
        raise AssertionError("Timed out waiting for " + label)

    def scan():
        tasks = jf("/ScheduledTasks", token=admin)
        for task in tasks:
            if task["Key"] in ("AniWorldMovieLibraryScan", "AniWorldSeriesLibraryScan"):
                wait_for(
                    lambda task_id=task["Id"]: (
                        next(
                            row
                            for row in jf("/ScheduledTasks", token=admin)
                            if row["Id"] == task_id
                        )["State"]
                        == "Idle"
                    ),
                    "idle scan",
                )
                old_result = next(
                    row
                    for row in jf("/ScheduledTasks", token=admin)
                    if row["Id"] == task["Id"]
                ).get("LastExecutionResult")
                jf(
                    "/ScheduledTasks/Running/" + task["Id"],
                    "POST",
                    token=admin,
                    expected=204,
                )
                finished = wait_for(
                    lambda task_id=task["Id"], previous=old_result: next(
                        (
                            row
                            for row in jf("/ScheduledTasks", token=admin)
                            if row["Id"] == task_id
                            and row["State"] == "Idle"
                            and row.get("LastExecutionResult") != previous
                        ),
                        None,
                    ),
                    "completed scan",
                )
                assert finished["LastExecutionResult"]["Status"] == "Completed", (
                    finished
                )

    try:
        libraries = {}
        for name, kind, root in (
            ("Movies-" + suffix, "movies", movies),
            ("Series-" + suffix, "tvshows", downloads),
        ):
            options = {
                "EnableRealtimeMonitor": False,
                "EnableInternetProviders": False,
                "TypeOptions": [
                    {"Type": value, "MetadataFetchers": [], "ImageFetchers": []}
                    for value in ("Movie", "Series", "Season", "Episode")
                ],
            }
            jf(
                "/Library/VirtualFolders?name="
                + name
                + "&collectionType="
                + kind
                + "&paths="
                + quote(str(root), safe=""),
                "POST",
                {"LibraryOptions": options},
                admin,
                204,
            )
            added_libraries.append(name)
            libraries[kind] = next(
                row
                for row in jf("/Library/VirtualFolders", token=admin)
                if row["Name"] == name
            )["ItemId"]
        config = jf("/Plugins/" + plugin_id + "/Configuration", token=admin)
        config.update(
            {
                "AniWorldUrl": "http://127.0.0.1:" + str(upstream.server_port),
                "AutoApproveRequests": True,
                "AllowedSources": site,
                "EnableAllUsers": False,
                "EnableMediaRouting": True,
                "MoviePathId": movie_path,
                "SeriesPathId": series_path,
                "MovieLibraryId": libraries["movies"],
                "SeriesLibraryId": libraries["tvshows"],
                "MovieLibraryPath": str(movies),
                "SeriesLibraryPath": str(downloads),
            }
        )
        # Save the connection before using its path verifier.
        initial = dict(config, EnableMediaRouting=False)
        jf("/Plugins/" + plugin_id + "/Configuration", "POST", initial, admin, 204)
        jf("/AniWorldRequests/Admin/ApiKey", "POST", {"ApiKey": raw_key}, admin)
        checked = jf("/AniWorldRequests/Admin/Targets/Validate", "POST", config, admin)
        config["MovieDownloadRoot"] = checked["movieDownloadRoot"]
        config["SeriesDownloadRoot"] = checked["seriesDownloadRoot"]
        jf("/Plugins/" + plugin_id + "/Configuration", "POST", config, admin, 204)
        jf("/AniWorldRequests/Admin/Targets", token=alice, expected=403)
        assert jf("/AniWorldRequests/Status", token=alice)["configured"]
        show_folder = downloads / (show + " (2026)")
        show_folder.mkdir()
        (show_folder / "tvshow.nfo").write_text(
            f"<tvshow><title>{show}</title><year>2026</year></tvshow>"
        )
        foreign = show_folder / "Season 01" / (show + " S01E099.mkv")
        foreign.parent.mkdir()
        shutil.copyfile(source_clips["English Dub"], foreign)
        scan()

        def plan_request(title, url, kind, user):
            jf(
                "/AniWorldRequests/Search?mediaType=video&source="
                + site
                + "&query="
                + quote(title),
                token=user,
            )
            selection = {
                "title": title,
                "seriesUrl": url,
                "source": site,
                "mediaType": kind,
            }
            plan = jf("/AniWorldRequests/Requests/Plan", "POST", selection, user)
            return selection, plan

        selection, plan = plan_request(show, series_url, "series", alice)
        assert plan["coverage"]["German Dub"]["available_count"] == 1
        assert plan["coverage"]["English Dub"]["available_count"] == 2
        selection.update(
            {
                "planToken": plan["plan_token"],
                "language": "English Dub",
                "provider": "VOE",
            }
        )
        result = jf("/AniWorldRequests/Requests/Automatic", "POST", selection, alice)
        request = result.get("request", result)
        queue_id = request.get("aniWorldQueueId") or result.get("aniWorldQueueId")
        assert queue_id, result
        worker._process(db.get_queue_item(queue_id))
        assert db.get_queue_item(queue_id)["status"] == "completed"
        jf("/AniWorldRequests/Requests/Progress", token=alice)
        scan()
        assert len(list(downloads.rglob("*english-dub.mkv"))) == 2
        assert not list(movies.rglob("*english-dub.mkv"))
        selection, plan = plan_request(show, series_url, "series", alice)
        assert plan["coverage"]["English Dub"]["existing_count"] == 2
        assert plan["coverage"]["German Dub"]["missing_count"] == 1
        selection.update({"language": "German Dub", "provider": "VOE"})
        jf("/AniWorldRequests/Requests/Automatic", "POST", selection, alice, 409)
        selection["allowPartial"] = True
        result = jf("/AniWorldRequests/Requests/Automatic", "POST", selection, alice)
        request = result.get("request", result)
        worker._process(db.get_queue_item(request["aniWorldQueueId"]))
        jf("/AniWorldRequests/Requests/Progress", token=alice)
        scan()
        _, verified = plan_request(show, series_url, "series", alice)
        assert verified["coverage"]["German Dub"]["existing_count"] == 1
        groups = jf("/AniWorldRequests/Downloads/Mine", token=alice)
        assert len(groups) == 1 and len(groups[0]["files"]) == 3
        assert all(item["verification"] == "verified" for item in groups[0]["files"])
        jf(
            "/AniWorldRequests/Downloads/" + groups[0]["id"] + "/Preview",
            "POST",
            token=bob,
            expected=404,
        )
        jf("/AniWorldRequests/Admin/Library", token=bob, expected=403)
        preview = jf(
            "/AniWorldRequests/Downloads/" + groups[0]["id"] + "/Preview",
            "POST",
            token=alice,
        )
        assert preview["count"] == 3
        jf(
            "/AniWorldRequests/Downloads/Delete",
            "POST",
            {"token": preview["token"]},
            bob,
            404,
        )
        deletion = jf(
            "/AniWorldRequests/Downloads/Delete",
            "POST",
            {"token": preview["token"]},
            alice,
        )
        assert all(row["status"] == "deleted" for row in deletion["items"])
        assert foreign.exists() and len(list(downloads.rglob("*.mkv"))) == 1
        scan()
        items = jf("/AniWorldRequests/Admin/Library?query=" + quote(show), token=admin)
        series_item = next(item for item in items if item["mediaType"] == "series")
        preview = jf(
            "/AniWorldRequests/Admin/Library/" + series_item["id"] + "/Preview",
            "POST",
            token=admin,
        )
        jf(
            "/AniWorldRequests/Admin/Library/Delete",
            "POST",
            {"token": preview["token"]},
            admin,
        )
        assert not foreign.exists()
        selection, plan = plan_request(film, film_url, "movie", bob)
        selection.update(
            {
                "planToken": plan["plan_token"],
                "language": "German Dub",
                "provider": "VOE",
            }
        )
        result = jf("/AniWorldRequests/Requests/Automatic", "POST", selection, bob)
        request = result.get("request", result)
        worker._process(db.get_queue_item(request["aniWorldQueueId"]))
        assert len(list(movies.rglob("*german-dub.mkv"))) == 1
        scan()
        assert any(
            item["mediaType"] == "movie"
            for item in jf(
                "/AniWorldRequests/Admin/Library?query=" + quote(film), token=admin
            )
        )
        groups = jf("/AniWorldRequests/Downloads/Mine", token=bob)
        preview = jf(
            "/AniWorldRequests/Downloads/" + groups[0]["id"] + "/Preview",
            "POST",
            token=bob,
        )
        jf(
            "/AniWorldRequests/Downloads/Delete",
            "POST",
            {"token": preview["token"]},
            bob,
        )
        assert not list(movies.rglob("*.mkv"))
    finally:
        for name in added_libraries:
            jf(
                "/Library/VirtualFolders?name=" + quote(name),
                "DELETE",
                token=admin,
                expected=204,
            )
        upstream.shutdown()
        thread.join(timeout=5)
