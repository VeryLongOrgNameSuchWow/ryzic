"""Tests for ``ryzic.commands.leave``."""

from __future__ import annotations

from typing import Any, cast

import pytest

from ryzic import lavalink_glue
from ryzic.commands import leave as leave_module
from tests._command_helpers import (
    FakeAudioTrack,
    FakeBot,
    FakeLavalinkClient,
    both_in_voice,
    context_for,
    install_lavalink_client,
)


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    lavalink_glue._reset_state_for_test()
    install_lavalink_client(None)


async def test_voice_precondition_short_circuits() -> None:
    bot = FakeBot()  # bot not in voice
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await leave_module._handle_leave(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "I'm not in a voice channel."
    assert bot.update_voice_state_calls == []


async def test_no_lavalink_client_returns_friendly_error() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    install_lavalink_client(None)

    await leave_module._handle_leave(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "I'm not in a voice channel."
    assert fake.responses[0][1].get("ephemeral") is True


async def test_no_player_returns_friendly_error() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    install_lavalink_client(FakeLavalinkClient())

    await leave_module._handle_leave(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "I'm not in a voice channel."
    assert bot.update_voice_state_calls == []


async def test_player_disconnected_returns_friendly_error() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = False

    await leave_module._handle_leave(ctx)

    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "I'm not in a voice channel."
    assert player.stop_calls == 0


async def test_leave_stops_clears_disconnects_and_responds() -> None:
    bot = both_in_voice()
    ctx = context_for(bot)
    ll = FakeLavalinkClient()
    install_lavalink_client(ll)
    player = ll.player_manager.create(guild_id=111)
    player.is_connected = True
    player.current = FakeAudioTrack(title="Now Playing")
    player.queue = [FakeAudioTrack(title="A"), FakeAudioTrack(title="B")]

    await leave_module._handle_leave(ctx)

    assert player.stop_calls == 1
    assert player.queue == []
    assert bot.update_voice_state_calls == [(111, None)]
    fake = cast(Any, ctx)
    assert fake.responses[0][0] == "Left voice channel. Queue cleared."
    assert "ephemeral" not in fake.responses[0][1]


def test_leave_loader_registered() -> None:
    assert leave_module.Leave._command_data.name == "leave"
