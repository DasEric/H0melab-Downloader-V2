"""Notification-only transport, tested without a live Discord account."""

import asyncio
import socket

_ORIGINAL_CONNECT = socket.socket.connect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from h0melab.web import discord_bot as bot


@pytest.fixture(autouse=True)
def local_event_loop(monkeypatch):
    # Windows creates its event-loop wakeup pipe using a loopback socket pair.
    # Permit only construction; all coroutine execution retains the network ban.
    with monkeypatch.context() as context:
        context.setattr(socket.socket, "connect", _ORIGINAL_CONNECT)
        loop = asyncio.new_event_loop()
    monkeypatch.setattr(asyncio, "run", loop.run_until_complete)
    yield
    loop.close()


@pytest.fixture
def transport(monkeypatch):
    marker = object()
    monkeypatch.setattr(
        bot,
        "discord",
        SimpleNamespace(AllowedMentions=SimpleNamespace(none=lambda: marker)),
        raising=False,
    )
    channel = SimpleNamespace(send=AsyncMock())
    user = SimpleNamespace(send=AsyncMock())
    client = SimpleNamespace(
        get_channel=lambda channel_id: channel,
        fetch_channel=AsyncMock(return_value=channel),
        fetch_user=AsyncMock(return_value=user),
    )
    return client, channel, user, marker


def test_event_suppresses_mentions_and_bounds_text(transport):
    client, channel, _, marker = transport
    asyncio.run(
        bot._event(
            client,
            {"announce_channel_id": "123"},
            "@everyone <@123> **title**" * 50,
            "completed",
        )
    )
    kwargs = channel.send.call_args.kwargs
    assert kwargs["allowed_mentions"] is marker
    assert "@" not in kwargs["content"] and "<" not in kwargs["content"]
    assert len(kwargs["content"]) < 650


def test_completion_preserves_old_job_dm(transport):
    client, channel, user, marker = transport
    asyncio.run(
        bot._announce(
            client,
            {"announce_channel_id": "123", "language": "de"},
            "@everyone",
            "movie",
            "German Dub",
            "456",
        )
    )
    client.fetch_user.assert_awaited_once_with(456)
    assert user.send.call_args.kwargs["allowed_mentions"] is marker
    assert "@" not in user.send.call_args.args[0]
    channel.send.assert_awaited_once()


def test_transport_failure_is_sanitized(transport, caplog):
    client, channel, _, _ = transport
    channel.send.side_effect = RuntimeError("secret-token")
    asyncio.run(bot._event(client, {"announce_channel_id": "123"}, "Title", "received"))
    assert "secret-token" not in caplog.text


def test_no_channel_no_message(transport):
    client, channel, _, _ = transport
    asyncio.run(bot._event(client, {}, "Title", "received"))
    channel.send.assert_not_called()


def test_disabled_notification_is_noop(monkeypatch):
    monkeypatch.setattr(bot, "_state", {"loop": None, "client": None, "config": None})
    assert bot.notify_event("Title", "received") is False
    assert bot.notify_completed("Title", "movie", "German Dub", "123") is False


def test_schedule_error_closes_coroutine(monkeypatch, caplog):
    monkeypatch.setattr(bot, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(
        bot,
        "_state",
        {
            "loop": SimpleNamespace(is_running=lambda: True),
            "client": object(),
            "config": {},
        },
    )
    coroutines = []

    def fail(coroutine, loop):
        coroutines.append(coroutine)
        raise RuntimeError("secret-token")

    monkeypatch.setattr(bot.asyncio, "run_coroutine_threadsafe", fail)
    assert bot.notify_event("Title", "received") is False
    assert coroutines[0].cr_frame is None
    assert "secret-token" not in caplog.text


def test_old_request_settings_are_not_consumed(monkeypatch):
    monkeypatch.setenv("H0MELAB_DISCORD_MODE", "advanced")
    monkeypatch.setenv("H0MELAB_DISCORD_OWNER_ID", "123")
    monkeypatch.setenv("H0MELAB_DISCORD_REQUEST_ROLE_ID", "456")
    assert not {"mode", "owner_id", "role_id"} & bot.read_config().keys()
    assert not hasattr(bot, "_enqueue")


def test_old_slash_commands_are_removed(monkeypatch):
    monkeypatch.setattr(
        bot, "discord", SimpleNamespace(Object=lambda **kw: kw), raising=False
    )
    from unittest.mock import Mock

    tree = SimpleNamespace(clear_commands=Mock(), sync=AsyncMock())
    client = SimpleNamespace(h0melab_tree=tree, h0melab_guild_id="123")
    asyncio.run(bot._clear_commands(client))
    assert tree.clear_commands.call_count == 2
    assert tree.sync.await_count == 2
