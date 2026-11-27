# SPDX-License-Identifier: GPL-3.0-or-later
#
# H0melab Soulseek service
#
# A small standalone program that keeps one Soulseek session alive with the
# aioslsk library (GPL-3.0-or-later) and offers it to the downloader over a
# local HTTP API. It runs as its own process in its own virtual environment
# and never imports anything from the downloader, which talks to it only over
# HTTP. Because it uses aioslsk, this file is licensed under the GNU General
# Public License, version 3 or later; see LICENSE in this directory.
#
# The routes mirror the subset of the slskd API the downloader already speaks
# (/api/v0/server, /searches, /transfers/downloads), plus Soulseek's own
# discovery data: global and personal recommendations and the room list.
#
# Usage: python service.py --config /path/to/service-config.json

import argparse
import asyncio
import datetime
import hmac
import json
import logging
import os
import sys
import threading
import time
import uuid
from importlib.metadata import version as package_version

from aiohttp import web
from aioslsk.client import SoulSeekClient
from aioslsk.commands import (
    AddInterestCommand,
    GetGlobalRecommendationsCommand,
    GetRecommendationsCommand,
    GetRoomListCommand,
)
from aioslsk.naming import (
    DefaultNamingStrategy,
    KeepDirectoryStrategy,
    NumberDuplicateStrategy,
)
from aioslsk.protocol.primitives import AttributeKey
from aioslsk.settings import (
    CredentialsSettings,
    Settings,
    SharedDirectorySettingEntry,
    SharesSettings,
)
from aioslsk.transfer.cache import TransferShelveCache
from aioslsk.transfer.state import TransferState

log = logging.getLogger("h0melab-soulseek")

SEARCH_SECONDS = 15
SEARCH_KEEP_SECONDS = 15 * 60
LOST_SESSION_SECONDS = 180
RECONNECT_DELAYS = (10, 30, 60, 120, 300)


class Service:
    def __init__(self, config):
        self.config = config
        self.client = None
        self.state = "Starting"
        self.error = ""
        self.searches = {}  # id -> (request, started_monotonic, text)
        self.transfer_ids = {}  # id(transfer) -> uuid
        self.requested_at = {}  # id(transfer) -> iso timestamp
        self._discover = {}  # key -> (time, data)

    # ------------------------------------------------------------------ setup
    def build_settings(self):
        cfg = self.config
        os.makedirs(cfg["download_dir"], exist_ok=True)
        shares = [SharedDirectorySettingEntry(path=path) for path in cfg.get("shared_dirs", []) if os.path.isdir(path)]
        settings = Settings(
            credentials=CredentialsSettings(username=cfg["username"], password=cfg["password"]),
            shares=SharesSettings(scan_on_start=True, download=cfg["download_dir"], directories=shares),
        )
        settings.network.listening.port = int(cfg.get("listen_port", 60000))
        settings.network.listening.obfuscated_port = int(cfg.get("listen_port", 60000)) + 1
        settings.network.upnp.enabled = bool(cfg.get("upnp", True))
        # aioslsk reconnects and logs back in by itself after short drops
        settings.network.server.reconnect.auto = True
        settings.network.server.reconnect.timeout = 15
        settings.interests.liked = set(cfg.get("liked", []))
        if cfg.get("server_host"):
            settings.network.server.hostname = cfg["server_host"]
            settings.network.server.port = int(cfg.get("server_port") or 2416)
        return settings

    async def run_client(self):
        """Connect and log in, retrying with a growing pause on failure.

        The Soulseek server bans clients that reconnect too quickly, so the
        delays are deliberately generous.
        """
        attempt = 0
        while True:
            try:
                data_dir = self.config["data_dir"]
                os.makedirs(data_dir, exist_ok=True)
                self.client = SoulSeekClient(
                    self.build_settings(),
                    transfer_cache=TransferShelveCache(data_dir),
                )
                # Take the remote file name first, then keep each album in its own
                # folder like the remote side has it; the order matters, without
                # the default strategy the name stays empty and only " (1)" is left
                self.client.shares.naming_strategies = [
                    DefaultNamingStrategy(), KeepDirectoryStrategy(), NumberDuplicateStrategy(),
                ]
                self.state = "Connecting"
                await self.client.start()
                await self.client.login()
                self.state = "Connected, LoggedIn"
                self.error = ""
                attempt = 0
                log.info("logged in to Soulseek as %s", self.config["username"])
                await self.wait_until_disconnected()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # any failure: report it and retry later
                self.error = f"{type(exc).__name__}: {exc}"
                log.warning("Soulseek session failed: %s", self.error)
            self.state = "Disconnected"
            if self.client is not None:
                try:
                    await self.client.stop()
                except Exception:
                    pass
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    @property
    def logged_in(self):
        return self.client is not None and getattr(self.client, "session", None) is not None

    async def wait_until_disconnected(self):
        """Watch the session; only rebuild the client if it stays gone.

        Short drops are healed by aioslsk itself (reconnect + login), so a
        full restart only happens after LOST_SESSION_SECONDS without a login.
        """
        lost_since = None
        while True:
            await asyncio.sleep(5)
            self.cleanup_searches()
            if self.logged_in:
                if lost_since is not None:
                    log.info("Soulseek session restored")
                lost_since = None
                self.state = "Connected, LoggedIn"
                self.error = ""
                continue
            if lost_since is None:
                log.warning("lost the Soulseek session, waiting for the automatic reconnect")
                lost_since = time.monotonic()
                self.state = "Connecting"
            if time.monotonic() - lost_since > LOST_SESSION_SECONDS:
                raise ConnectionError("the Soulseek server connection did not come back")

    def cleanup_searches(self):
        now = time.monotonic()
        for search_id, (request, started, _) in list(self.searches.items()):
            if now - started > SEARCH_KEEP_SECONDS:
                self.searches.pop(search_id, None)
                try:
                    self.client.searches.remove_request(request)
                except Exception:
                    pass

    def require_session(self):
        if self.client is None or not self.logged_in:
            raise web.HTTPServiceUnavailable(
                text=json.dumps({"error": "Soulseek is not connected yet"}),
                content_type="application/json",
            )

    # ------------------------------------------------------------- transfers
    def transfer_id(self, transfer):
        key = id(transfer)
        if key not in self.transfer_ids:
            self.transfer_ids[key] = str(uuid.uuid4())
            self.requested_at.setdefault(key, datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"))
        return self.transfer_ids[key]

    @staticmethod
    def slskd_state(transfer):
        value = transfer.state.VALUE
        if value == TransferState.COMPLETE:
            return "Completed, Succeeded"
        if value == TransferState.FAILED:
            return "Completed, Errored"
        if value == TransferState.ABORTED:
            return "Completed, Cancelled"
        if value == TransferState.DOWNLOADING:
            return "InProgress"
        if value == TransferState.INITIALIZING:
            return "Initializing"
        if value == TransferState.PAUSED:
            return "Queued, Paused"
        return "Queued, Remotely" if transfer.remotely_queued else "Queued, Locally"

    def transfer_json(self, transfer):
        size = transfer.filesize or 0
        done = transfer.bytes_transfered or 0
        try:
            speed = transfer.get_speed()
        except Exception:
            speed = 0.0
        return {
            "id": self.transfer_id(transfer),
            "username": transfer.username,
            "filename": transfer.remote_path,
            "size": size,
            "state": self.slskd_state(transfer),
            "bytesTransferred": done,
            "percentComplete": round(done / size * 100, 1) if size else 0,
            "averageSpeed": speed,
            "placeInQueue": transfer.place_in_queue,
            "requestedAt": self.requested_at.get(id(transfer), ""),
            "localPath": transfer.local_path or "",
            "failReason": transfer.fail_reason or "",
        }

    def downloads(self):
        if self.client is None:
            return []
        return self.client.transfers.get_downloads()

    def find_download(self, username, transfer_id):
        for transfer in self.downloads():
            if transfer.username == username and self.transfer_id(transfer) == transfer_id:
                return transfer
        return None

    def grouped_downloads(self, username=None):
        users = {}
        for transfer in self.downloads():
            if username is not None and transfer.username != username:
                continue
            directory = transfer.remote_path.replace("/", "\\").rpartition("\\")[0]
            users.setdefault(transfer.username, {}).setdefault(directory, []).append(self.transfer_json(transfer))
        return [
            {
                "username": user,
                "directories": [
                    {"directory": directory, "fileCount": len(files), "files": files}
                    for directory, files in directories.items()
                ],
            }
            for user, directories in users.items()
        ]

    # ---------------------------------------------------------------- discover
    async def cached(self, key, seconds, factory):
        now = time.monotonic()
        hit = self._discover.get(key)
        if hit and now - hit[0] < seconds:
            return hit[1]
        data = await factory()
        self._discover[key] = (now, data)
        return data

    async def recommendations(self, personal):
        self.require_session()
        command = GetRecommendationsCommand() if personal else GetGlobalRecommendationsCommand()
        liked, _ = await self.client.execute(command, response=True, timeout=15)
        items = sorted(liked or [], key=lambda rec: rec.score, reverse=True)
        return [{"item": rec.recommendation, "score": rec.score} for rec in items if rec.recommendation]

    async def rooms(self):
        self.require_session()
        rooms = await self.client.execute(GetRoomListCommand(), response=True, timeout=15)
        rows = [{"name": room.name, "userCount": room.user_count} for room in rooms or [] if not room.private]
        rows.sort(key=lambda row: row["userCount"], reverse=True)
        return rows


# ---------------------------------------------------------------------- routes
def json_body(data, status=200):
    return web.json_response(data, status=status)


def build_app(service, token):
    @web.middleware
    async def auth(request, handler):
        given = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(given, token):
            return json_body({"error": "unauthorized"}, 401)
        return await handler(request)

    app = web.Application(middlewares=[auth])
    routes = web.RouteTableDef()

    @routes.get("/api/v0/server")
    async def server(_request):
        return json_body({
            "state": service.state,
            "isConnected": service.state.startswith("Connected"),
            "isLoggedIn": service.logged_in,
            "username": service.config["username"],
            "version": package_version("aioslsk"),
            "error": service.error,
        })

    @routes.post("/api/v0/searches")
    async def start_search(request):
        service.require_session()
        body = await request.json()
        text = " ".join(str(body.get("searchText") or "").split())
        if len(text) < 2:
            return json_body({"error": "search text too short"}, 400)
        search_id = str(body.get("id") or uuid.uuid4())
        search = await service.client.searches.search(text)
        service.searches[search_id] = (search, time.monotonic(), text)
        return json_body({"id": search_id, "searchText": text})

    def get_search(request):
        entry = service.searches.get(request.match_info["search_id"])
        if entry is None:
            raise web.HTTPNotFound(text='{"error": "unknown search"}', content_type="application/json")
        return entry

    @routes.get("/api/v0/searches/{search_id}")
    async def search_state(request):
        search, started, text = get_search(request)
        return json_body({
            "searchText": text,
            "isComplete": time.monotonic() - started >= SEARCH_SECONDS,
            "responseCount": len(search.results),
            "fileCount": sum(len(result.shared_items) for result in search.results),
        })

    @routes.get("/api/v0/searches/{search_id}/responses")
    async def search_responses(request):
        search, _, _ = get_search(request)
        rows = []
        for result in list(search.results):
            files = []
            for item in result.shared_items:
                attrs = item.get_attribute_map()
                files.append({
                    "filename": item.filename,
                    "size": item.filesize,
                    "extension": item.extension,
                    "bitRate": attrs.get(AttributeKey.BITRATE),
                    "length": attrs.get(AttributeKey.DURATION),
                    "sampleRate": attrs.get(AttributeKey.SAMPLE_RATE),
                    "bitDepth": attrs.get(AttributeKey.BIT_DEPTH),
                })
            rows.append({
                "username": result.username,
                "hasFreeUploadSlot": bool(result.has_free_slots),
                "uploadSpeed": result.avg_speed,
                "queueLength": result.queue_size,
                "files": files,
            })
        return json_body(rows)

    @routes.delete("/api/v0/searches/{search_id}")
    async def delete_search(request):
        entry = service.searches.pop(request.match_info["search_id"], None)
        if entry and service.client:
            try:
                service.client.searches.remove_request(entry[0])
            except Exception:
                pass
        return web.Response(status=204)

    @routes.get("/api/v0/transfers/downloads")
    async def downloads(_request):
        return json_body(service.grouped_downloads())

    @routes.delete("/api/v0/transfers/downloads/all/completed")
    async def clear_completed(_request):
        for transfer in service.downloads():
            if transfer.is_finalized():
                await service.client.transfers.remove(transfer)
        return web.Response(status=204)

    @routes.get("/api/v0/transfers/downloads/{username}")
    async def user_downloads(request):
        found = service.grouped_downloads(request.match_info["username"])
        if not found:
            return json_body({"error": "no downloads"}, 404)
        return json_body(found[0])

    @routes.post("/api/v0/transfers/downloads/{username}")
    async def enqueue(request):
        service.require_session()
        username = request.match_info["username"]
        body = await request.json()
        if not isinstance(body, list) or not body:
            return json_body({"error": "no files"}, 400)
        for entry in body:
            filename = str(entry.get("filename") or "")
            if not filename:
                continue
            existing = next(
                (t for t in service.downloads() if t.username == username and t.remote_path == filename),
                None,
            )
            if existing is not None and existing.is_finalized():
                await service.client.transfers.remove(existing)
            elif existing is not None:
                continue
            transfer = await service.client.transfers.download(username, filename)
            if entry.get("size"):
                transfer.filesize = transfer.filesize or int(entry["size"])
            service.transfer_id(transfer)
        return json_body({}, 201)

    @routes.delete("/api/v0/transfers/downloads/{username}/{transfer_id}")
    async def cancel(request):
        transfer = service.find_download(request.match_info["username"], request.match_info["transfer_id"])
        if transfer is None:
            return json_body({"error": "unknown transfer"}, 404)
        if not transfer.is_finalized():
            try:
                await service.client.transfers.abort(transfer)
            except Exception:
                pass
        if request.query.get("remove") == "true":
            await service.client.transfers.remove(transfer)
        return web.Response(status=204)

    @routes.get("/api/v0/discover/recommendations")
    async def global_recommendations(_request):
        data = await service.cached("global", 30 * 60, lambda: service.recommendations(False))
        return json_body(data)

    @routes.get("/api/v0/discover/recommendations/personal")
    async def personal_recommendations(_request):
        data = await service.cached("personal", 5 * 60, lambda: service.recommendations(True))
        return json_body(data)

    @routes.get("/api/v0/discover/rooms")
    async def rooms(_request):
        data = await service.cached("rooms", 30 * 60, service.rooms)
        return json_body(data)

    @routes.post("/api/v0/interests")
    async def like(request):
        service.require_session()
        body = await request.json()
        item = " ".join(str(body.get("item") or "").split())[:80]
        if not item:
            return json_body({"error": "empty interest"}, 400)
        await service.client.execute(AddInterestCommand(item))
        service.client.settings.interests.liked.add(item)
        service._discover.pop("personal", None)
        return json_body({"item": item}, 201)

    app.add_routes(routes)
    return app


async def main(config):
    service = Service(config)
    app = build_app(service, config["api_token"])
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", int(config["api_port"]))
    await site.start()
    log.info("API listening on 127.0.0.1:%s", config["api_port"])
    client_task = asyncio.create_task(service.run_client())
    stop = asyncio.Event()
    try:
        import signal

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
    except (NotImplementedError, AttributeError, ValueError):
        pass  # Windows: the parent terminates the process instead
    if config.get("watch_parent"):
        # The downloader keeps our stdin open; EOF means it is gone, even
        # when it was killed hard, so this process never outlives it.
        def watch_stdin():
            try:
                while sys.stdin.buffer.read(1024):
                    pass
            except (OSError, ValueError):
                pass
            loop.call_soon_threadsafe(stop.set)

        loop = asyncio.get_running_loop()
        threading.Thread(target=watch_stdin, name="parent-watch", daemon=True).start()
    try:
        await stop.wait()
    finally:
        client_task.cancel()
        if service.client is not None:
            try:
                await service.client.stop()
            except Exception:
                pass
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="H0melab Soulseek service")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aioslsk").setLevel(logging.WARNING)
    with open(args.config, encoding="utf-8") as handle:
        loaded = json.load(handle)
    asyncio.run(main(loaded))
