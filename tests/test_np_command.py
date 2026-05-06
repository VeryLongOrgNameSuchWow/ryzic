"""Tests for ``ryzic.commands.np``."""

from __future__ import annotations

from typing import Any, cast

import hikari
import pytest

from ryzic import lavalink_glue
from ryzic.commands import np as np_module
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
    ctx = context_for(bot, guild_id=None)

    await np_module._handle_np(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Run /np in a server."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_no_lavalink_client_returns_nothing_playing() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    install_lavalink_client(None)

    await np_module._handle_np(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Nothing is playing."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_no_player_returns_nothing_playing() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await np_module._handle_np(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Nothing is playing."


async def test_player_idle_returns_nothing_playing() -> None:
    bot = FakeBot()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.current = None

    await np_module._handle_np(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Nothing is playing."


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
    assert fake.responses[0][0] == "Nothing is playing."


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


def test_loader_registered_np_command() -> None:
    assert np_module.NowPlaying._command_data.name == "np"
