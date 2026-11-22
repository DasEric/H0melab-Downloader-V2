"""Optional, notification-only Discord transport. Requests are handled by Seerr.

Old slash commands are removed on connection; historical queued jobs can still
send completion DMs. Notification failures never interrupt download processing.
"""
import asyncio
import os
import re
import threading

from ..logger import get_logger

logger = get_logger(__name__)
try:
    import discord
    from discord import app_commands
    DISCORD_AVAILABLE = True
except Exception:
    DISCORD_AVAILABLE = False

_state = {"thread": None, "loop": None, "client": None, "running": False,
          "error": None, "user": None, "config": None}
_lock = threading.Lock()


def read_config():
    return {
        "enabled": os.environ.get("H0MELAB_DISCORD_BOT_ENABLED", "0") == "1",
        "token": os.environ.get("H0MELAB_DISCORD_TOKEN", "").strip(),
        "guild_id": os.environ.get("H0MELAB_DISCORD_GUILD_ID", "").strip(),
        "language": os.environ.get("H0MELAB_DISCORD_LANGUAGE", "en").strip().lower(),
        "announce_channel_id": os.environ.get("H0MELAB_DISCORD_ANNOUNCE_CHANNEL_ID", "").strip(),
    }


def get_status():
    with _lock:
        return {"available": DISCORD_AVAILABLE, "running": _state["running"],
                "error": _state["error"], "user": _state["user"]}


def _text(value, limit=400):
    # Keep user-controlled metadata plain and bounded; suppress links/mentions.
    value = str(value or "")
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    value = value.replace("@", "[at]")
    value = re.sub(r"[<>*_`~|]", "", value)
    return value[:limit]


def _schedule(factory):
    """Construct a coroutine only when a usable loop exists, and close on error."""
    coroutine = None
    try:
        with _lock:
            loop, client, config = _state["loop"], _state["client"], _state["config"]
        if not (DISCORD_AVAILABLE and loop and client and loop.is_running()):
            return False
        coroutine = factory(client, config or {})
        asyncio.run_coroutine_threadsafe(coroutine, loop)
        return True
    except Exception:
        if coroutine is not None:
            coroutine.close()
        logger.info("Discord: notification scheduling failed")
        return False


def notify_event(title, event):
    """Best-effort Seerr event announcement; deduplication belongs to its ledger."""
    return _schedule(lambda client, config: _event(client, config, title, event))


async def _event(client, config, title, event):
    try:
        channel_id = config.get("announce_channel_id")
        if not channel_id:
            return
        channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
        await channel.send(
            content=f"Seerr: {_text(title)} - {_text(event, 200)}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        logger.info("Discord: event announcement failed")


def notify_completed(title, media_type, language, discord_user_id):
    """Preserve completion notices for historical jobs, without accepting requests."""
    return _schedule(lambda client, config: _announce(
        client, config, title, media_type, language, discord_user_id))


async def _announce(client, config, title, media_type, language, user_id):
    german = config.get("language") == "de"
    title, language = _text(title), _text(language, 80)
    message = (f"{title} ({language}) ist fertig heruntergeladen und jetzt verfuegbar."
               if german else f"{title} ({language}) has finished downloading and is now available.")
    if user_id:
        try:
            user = await client.fetch_user(int(user_id))
            await user.send(message, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.info("Discord: completion DM failed")
    channel_id = config.get("announce_channel_id")
    if not channel_id:
        return
    try:
        channel = client.get_channel(int(channel_id)) or await client.fetch_channel(int(channel_id))
        await channel.send(content=message, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        logger.info("Discord: completion announcement failed")


def _build_client(config):
    client = discord.Client(intents=discord.Intents.default(),
                            allowed_mentions=discord.AllowedMentions.none())
    # An empty tree replaces old request commands on Discord itself.
    client.h0melab_tree = app_commands.CommandTree(client)
    client.h0melab_guild_id = config["guild_id"]

    @client.event
    async def on_ready():
        with _lock:
            _state.update(running=True, error=None, user=str(client.user))
        try:
            await _clear_commands(client)
        except Exception:
            logger.warning("Discord: removing old request commands failed")
        logger.info("Discord notification client ready")

    return client


def _run_bot(config):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    with _lock:
        _state["loop"] = loop

    client = _build_client(config)
    with _lock:
        _state["client"] = client

    try:
        loop.run_until_complete(client.start(config["token"]))
    except Exception:
        logger.error("Discord notification client stopped")
        with _lock:
            _state["error"] = "Discord connection failed"
    finally:
        try:
            loop.run_until_complete(client.close())
        except Exception:
            pass
        loop.close()
        with _lock:
            _state.update(running=False, loop=None, client=None, user=None)


async def _clear_commands(client):
    tree = getattr(client, "h0melab_tree", None)
    if tree is None:
        return
    tree.clear_commands(guild=None)
    await tree.sync()
    guild_id = getattr(client, "h0melab_guild_id", "")
    if guild_id:
        guild = discord.Object(id=int(guild_id))
        tree.clear_commands(guild=guild)
        await tree.sync(guild=guild)


def _stop_locked():
    client, loop, thread = _state["client"], _state["loop"], _state["thread"]
    if client and loop and loop.is_running():
        # Unregister the slash commands first so nothing lingers while it is off
        try:
            asyncio.run_coroutine_threadsafe(_clear_commands(client), loop).result(
                timeout=20
            )
        except Exception:
            logger.warning("Discord: clearing commands on stop failed")
        asyncio.run_coroutine_threadsafe(client.close(), loop)
    if thread and thread.is_alive():
        thread.join(timeout=10)
    _state.update(thread=None, running=False, config=None)


def reconcile():
    """Start, stop or restart the bot so it matches the current configuration."""
    if not DISCORD_AVAILABLE:
        with _lock:
            _state["error"] = "discord.py not installed"
        return

    config = read_config()
    with _lock:
        running = _state["thread"] is not None and _state["thread"].is_alive()
        should_run = config["enabled"] and bool(config["token"])

        if not should_run:
            if running:
                _stop_locked()
            _state["error"] = None
            return

        if running and _state["config"] == config:
            return
        if running:
            _stop_locked()

        _state["config"] = config
        _state["error"] = None
        thread = threading.Thread(target=_run_bot, args=(config,), daemon=True)
        _state["thread"] = thread
        thread.start()


def start_if_enabled():
    try:
        reconcile()
    except Exception:
        logger.error("Discord notification client start failed")
