"""Tests for ``ryzic.commands.np``."""

from __future__ import annotations

from typing import Any, cast

import hikari
import pytest

from ryzic import lavalink_glue
from ryzic.commands import np as np_module
from ryzic.i18n import t
from tests._command_helpers import (
    FakeAudioTrack,
    FakeBot,
    FakeLavalinkClient,
    context_for,
    install_lavalink_client,
    make_track_with_info,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    lavalink_glue._reset_state_for_test()
    install_lavalink_client(None)


async def test_dm_invocation_returns_friendly_error() -> None:
    bot = FakeBot()
    ctx = context_for(bot, guild_id=None, command_name="nowplaying")

    await np_module._handle_np(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t(
        "voice.error.run_in_server", locale="en_US", command="nowplaying"
    )
    assert fake.responses[0][0] == "Run /nowplaying in a server."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_dm_invocation_uses_command_name_dynamically() -> None:
    """The ``/np`` alias surfaces the alias name in the error copy."""
    bot = FakeBot()
    ctx = context_for(bot, guild_id=None, command_name="np")

    await np_module._handle_np(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Run /np in a server."


async def test_no_lavalink_client_returns_nothing_playing() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    install_lavalink_client(None)

    await np_module._handle_np(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("np.error.nothing_playing", locale="en_US")
    assert fake.responses[0][0] == "Nothing is playing."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_no_player_returns_nothing_playing() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await np_module._handle_np(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("np.error.nothing_playing", locale="en_US")


async def test_player_idle_returns_nothing_playing() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = None

    await np_module._handle_np(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("np.error.nothing_playing", locale="en_US")


async def test_current_without_metadata_returns_nothing_playing() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    # Track without attached TrackInfo extras → treated as not-playable for /np.
    player.current = cast(Any, FakeAudioTrack())

    await np_module._handle_np(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == t("np.error.nothing_playing", locale="en_US")


async def test_responds_with_now_playing_embed_when_playing() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    track = make_track_with_info(title="Hello", duration_ms=240_000)
    player.current = cast(Any, track)
    player.position = 60_000
    player.paused = False

    await np_module._handle_np(ctx)

    fake = cast(Any, ctx)
    embed = fake.responses[0][1].get("embed")
    assert isinstance(embed, hikari.Embed)
    assert embed.title == "Now playing"
    body = embed.description or ""
    assert "Hello" in body
    assert "1:00 / 4:00" in body


async def test_paused_state_renders_in_embed() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = cast(Any, make_track_with_info(title="Hello", duration_ms=240_000))
    player.position = 30_000
    player.paused = True

    await np_module._handle_np(ctx)

    fake = cast(Any, ctx)
    embed = fake.responses[0][1].get("embed")
    body = (embed.description or "") if isinstance(embed, hikari.Embed) else ""
    assert "(paused)" in body


async def test_default_response_is_ephemeral() -> None:
    """``/np`` defaults to ``private=True`` (issue #207, aligned with ``/queue`` #148).

    Status-introspection commands answer the invoker, not the channel.
    """
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = cast(Any, make_track_with_info(title="Hello"))

    await np_module._handle_np(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][1].get("ephemeral") is True


async def test_private_false_makes_response_public() -> None:
    """``private=False`` opts back to a public response (issue #100)."""
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = cast(Any, make_track_with_info(title="Hello"))

    await np_module._handle_np(ctx, private=False)

    fake = cast(Any, ctx)
    assert fake.responses[0][1].get("ephemeral") is False
    embed = fake.responses[0][1]["embed"]
    assert isinstance(embed, hikari.Embed)


async def test_private_true_makes_response_ephemeral() -> None:
    """``private=True`` (also the default since #207) routes to ephemeral."""
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = cast(Any, make_track_with_info(title="Hello"))

    await np_module._handle_np(ctx, private=True)

    fake = cast(Any, ctx)
    assert fake.responses[0][1].get("ephemeral") is True
    embed = fake.responses[0][1]["embed"]
    assert isinstance(embed, hikari.Embed)


def test_loader_registered_nowplaying_command() -> None:
    """``/nowplaying`` is the primary surface registered by the loader."""
    assert np_module.NowPlaying._command_data.name == "nowplaying"
    assert np_module.NowPlaying._command_data.description == t(
        "np.command.description", locale="en_US"
    )
    assert np_module.NowPlaying._command_data.description == "Show what's playing right now."


def test_loader_registered_np_alias() -> None:
    """``/np`` remains as a shorthand alias to ``/nowplaying`` (issue #150)."""
    assert np_module.Np._command_data.name == "np"
    assert np_module.Np._command_data.description == t("np.command.description", locale="en_US")


def test_nowplaying_option_private_defaults_to_true() -> None:
    """``/nowplaying``'s ``private`` option defaults to ``True`` (issue #207).

    Mirrors the option-default assertion #212 established for ``/queue``.
    """
    assert np_module.NowPlaying._command_data.options["private"].default is True


def test_np_alias_option_private_defaults_to_true() -> None:
    """``/np`` alias's ``private`` option defaults to ``True`` (issue #207)."""
    assert np_module.Np._command_data.options["private"].default is True
